"""Literate config — DB ↔ ~/org/org-llm-config.org round-trip.

Mirrors every "user-tweakable" config key from the SQLite `config`
table to a single org file you can edit by hand, version-control, or
read alongside your other notes. The org file is the SAME shape as
context.org / llm-history.org — one heading per key, a PROPERTIES
drawer with metadata, and a `#+begin_src text :tangle …` block whose
content IS the value.

Two sync directions:

  tangle_db_to_org()   — write the org file from current DB state
  apply_org_to_db()    — read the org file, write changes back to DB
  diff_db_vs_org()     — show what would change in either direction

What's INCLUDED:
  • Every key in MODEL_DEFAULTS that the user is meant to tweak
    (chat_model, embed_model, ollama_url, theme dials, doctor knobs,
    log knobs, auto_embed knobs, etc.)
  • Custom keys the user has added to the config table.

What's EXCLUDED:
  • Runtime state masquerading as config (cloud_usage = a JSON event
    log; user_theme_knobs = managed via `org-llm knob`). These are
    too structured + churn too much for a literate file.
  • db_version (internal; round-tripping it is a footgun).

Auto-sync on writes is opt-in via `config_org_autosync` — when true,
every set_config call also re-tangles the org file. Off by default
because writing files on every config tweak feels surprising.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_LITERATE_PATH = Path("~/org/org-llm-config.org").expanduser()
_TANGLE_DIR    = Path("~/.local/share/org-llm/config").expanduser()


# Keys that round-trip cleanly. JSON state + internal counters excluded.
# user_theme_knobs IS round-trippable but lives in its own dedicated
# section of the literate file with one heading per knob — see
# `_render_knobs_section` / `_parse_knobs_from_org`.
EXCLUDED_KEYS = {
    "cloud_usage",        # event log, not config
    "user_theme_knobs",   # rendered separately as Theme Knobs heading tree
    "db_version",         # internal
}


# Tiny human-readable description per key. Falls back to "(no description)"
# for keys not listed here. Keep these short — the org heading title
# already holds the key name.
KEY_DESCRIPTIONS = {
    "org_dir":           "Where your org-roam vault lives.",
    "ollama_url":        "Local Ollama API URL (default :11434).",
    "embed_model":       "Model used for vector embeddings.",
    "chat_model":        "Model used for ask / chat.",
    "code_model":        "Model used for code generation.",
    "reason_model":      "Model used for planning + complex reasoning.",
    "fast_model":        "Model used for tagging + classification.",
    "instruct_model":    "Model used for capture + instruction following.",
    "text_model":        "Model used for summarization + text analysis.",
    "review_model":      "Model used by `org-llm review-emacs`.",
    "tag_model":         "Override model used by `org-llm tag` (else fast_model).",
    "fixer_model":       "Cloud model used for SRE-style fix recovery.",
    "embed_dim":         "Embedding dimensionality (matches your embed_model).",
    "context_window":    "Default context window in tokens.",
    "temperature":       "Default chat sampling temperature.",
    "top_p":             "Default chat top-p sampling.",
    "code_dirs":         "Comma-separated paths for `code-index`.",
    "theme":             "UI color mode: dark | light.",
    "lcars_palette":     "LCARS color palette: classic | red | green | gold | violet.",
    "lcars_color_primary":   "Hex override for the primary LCARS channel (e.g. #FF9900).",
    "lcars_color_secondary": "Hex override for the secondary LCARS channel.",
    "lcars_color_tertiary":  "Hex override for the tertiary LCARS channel.",
    "theme_cross_references_level":
        "How aggressively the LLM finds overlaps between knobs "
        "(0=off, 1=sparse, 2=normal, 3=max). e.g. trek+commie+queer at "
        "level 3 produces 'Worf's labor solidarity', 'queer joy in the "
        "holodeck' rather than three separate single-voice lines.",
    "trek_level":        "Star Trek voice intensity (0-3).",
    "commie_level":      "Solidarity / collective-action voice (0-3).",
    "queer_level":       "Pride / care voice (0-3).",
    "cloud_provider":    "Active cloud provider slug (e.g. openrouter).",
    "cloud_endpoint_url":"Cloud provider's OpenAI-compatible endpoint.",
    "cloud_model":       "Cloud model name to route chat through.",
    "doctor_proactive_mode":   "off | passive | active | aggressive.",
    "doctor_stuck_threshold":  "N tool calls before LLM self-doctors.",
    "doctor_intervene_in":     "Comma-sep triggers for proactive_doctor.",
    "doctor_auto_apply":       "True = power-boost applies without --apply.",
    "log_level":               "off | minimal | normal | verbose.",
    "log_kinds":               "Comma-sep event kinds the logbook records.",
    "log_max_rows_per_kind":   "Per-kind cap in the History table.",
    "log_auto_reflect_every":  "Run LLM reflection every Nth invocation. 0 = off.",
    "auto_embed_enabled":      "Background auto-embedder daemon thread.",
    "auto_embed_interval_secs":"Watcher poll interval (≥15s).",
    "auto_embed_quiet":        "Suppress per-batch terminal output.",
    "config_org_autosync":     "Re-tangle org-llm-config.org on every set.",
    # ── opencode TUI sidebar (Phase 17.1) ─────────────────────────────
    "sidebar_panel_enabled":
        "Master switch for the LCARS sidebar panel. False = plugin "
        "registers branding only; sidebar_content + home_bottom slots "
        "stay vacant and internal opencode panels render unmodified.",
    "sidebar_panel_on_home":
        "Render the panel via home_bottom on opencode's welcome screen "
        "(visible immediately on launch, before any chat). False = "
        "panel only appears in session view.",
    "sidebar_panel_on_session":
        "Render the panel via sidebar_content in chat sessions. False "
        "= panel only appears on home, or not at all if both are off.",
    "sidebar_sections":
        "Comma-sep section order. Tokens: vault, active, model, "
        "subsystems, life-support, archive, engage. Drop to hide; "
        "reorder freely. e.g. 'vault,engage' for a minimal panel.",
    "sidebar_refresh_secs":
        "How often the TS plugin re-reads .opencode/sidebar-status.json. "
        "Clamped to ≥5s. 15s is the launch-time write cadence; polling "
        "faster only matters once an auto-embedder daemon refreshes "
        "the file mid-session.",
    "sidebar_panel_width":
        "Width (cols) of the home_bottom panel. sidebar_content uses "
        "opencode's native sidebar width regardless of this value.",
    "sidebar_replace_internal":
        "Comma-sep list of opencode internal sidebar plugin IDs (no "
        "'internal:' prefix) to deactivate so our LCARS panel owns "
        "the slot. Empty = keep them all. sidebar-context is intent"
        "ionally omitted from the default — it carries token usage "
        "+ session cost.",
    "sidebar_top_tags_count":
        "Number of top tags to render in the ARCHIVE section.",
    "sidebar_activity_window_days":
        "Window (days) for the ARCHIVE section's 'recent nodes / files' "
        "counts.",
    "sidebar_alert_window_hours":
        "Window (hours) for the LIFE SUPPORT section's recent SensorLog "
        "alerts.",
    "sidebar_alert_limit":
        "Max number of recent alerts surfaced in LIFE SUPPORT.",
    "sidebar_make_it_so":
        "Show the 'MAKE IT SO' footer line. Pure flavor — set false "
        "for a cleaner panel.",
    "sidebar_stardate_show":
        "Show the 'STARDATE 8xxxx.x' header. Pure flavor.",
    "sidebar_auto_session":
        "Auto-create a chat session at launch by submitting a "
        "minimal opening prompt. opencode then navigates to the "
        "session route, which renders the full LCARS sidebar in "
        "sidebar_content. False = user must type something before "
        "the sidebar appears (the home_bottom one-line summary "
        "still shows in either case).",
    "sidebar_auto_session_prompt":
        "Auto-session opening prompt. Empty (default) → cli.py "
        "renders a Trek-themed LCARS banner with live stardate + "
        "vault stats + a 'Hailing frequencies open' question. The "
        "banner persists in the chat history, preserving org-llm "
        "identity after the welcome screen swaps to the session "
        "view. Set to any literal string to override (verbatim, "
        "no templating).",
    "sidebar_auto_session_delay_ms":
        "Delay (ms) before the plugin auto-submits the opening "
        "prompt. 0 (default) submits instantly so the user doesn't "
        "see a placeholder-rotating prompt that's about to be "
        "overwritten. Higher values give a longer welcome reveal "
        "but the prompt remains rendered during the wait.",
    "sidebar_auto_session_local_model":
        "Local Ollama model used when the launch is forced into "
        "local mode. Ignored when running cloud. Default is a "
        "tool-capable model so MCP tools (search_notes, etc.) work "
        "out of the box; some local models like gemma3 are text-"
        "only and reject tool calls.",
    "sidebar_auto_session_use_cloud":
        "Allow the auto-session to use cloud LLMs. False (default) "
        "forces the launch to route chat through local Ollama "
        "regardless of cloud config — so the auto-prompt doesn't "
        "fire a cloud call (cost, latency, privacy). Switch to true "
        "to use whichever model your launch is otherwise configured "
        "for. Note: opencode doesn't switch models mid-session, so "
        "this affects the WHOLE session — not just the first message.",
    "sidebar_slow_llm_threshold_ms":
        "How long (ms) the plugin waits for the AI to start "
        "responding before auto-running proactive_doctor and "
        "surfacing its diagnosis. Catches stuck-thinking states "
        "the LLM-self-doctor can't (it has no introspection while "
        "generating tokens). Set to 0 to disable the watcher.",
    "sidebar_prompt_char":
        "Trek-themed prompt prefix character rendered before the "
        "input box on the welcome screen. e.g. '▶ ' (default), "
        "'✦ ', '› ', '❯ ', '★ '. Empty string disables.",
    "sidebar_slow_llm_auto_relaunch":
        "Whether the slow-LLM watcher should auto-relaunch opencode "
        "after applying the cloud-routing fix. True (default) "
        "spawns a detached `org-llm launch` and exits the current "
        "opencode; the new instance auto-resubmits the stashed "
        "pending prompt via cloud. False keeps the manual flow "
        "(user exits + reruns).",
    "proxy_cloud_failover_enabled":
        "Master switch for the proxy's cloud-failover path. When "
        "true (default), a chat-completions request that stalls "
        "past `proxy_first_byte_timeout_ms` is silently re-issued "
        "against the configured cloud provider for this request "
        "only. False = local stalls surface as a 502 to opencode.",
    "proxy_first_byte_timeout_ms":
        "Time-to-first-byte threshold (ms) for cloud failover. "
        "0 disables failover entirely (equivalent to flipping the "
        "master switch off). After the first byte arrives the socket "
        "timeout is extended to 300s so legitimate slow streaming "
        "runs uninterrupted. Tune against the Phase 17 audit's p95.",
    "proxy_cloud_first":
        "When true, chat completions skip the local upstream and "
        "route directly to cloud — saving the per-request "
        "proxy_first_byte_timeout_ms wait. Useful when local "
        "hardware can't realistically serve the configured "
        "chat_model in time (low free RAM, no GPU, thermal "
        "throttling). The cloud-failover retry chain "
        "(compressed-tools → no-tools) still applies on context "
        "overflow. Default false. Onboarding step 6c offers this "
        "automatically when free RAM < 1.2× the chat_model "
        "footprint AND a cloud provider is configured.",
    "cloud_catalog_auto_refresh_enabled":
        "Background catalog refresh on every launch (Phase 18). True "
        "(default) spawns a daemon thread that polls OpenRouter for "
        "new cloud models and merges them into the user cache; the "
        "user discovers new flagship models without running "
        "`cloud --refresh-catalog` manually. False = catalog is "
        "frozen at whatever the bundled JSON ships with.",
    "cloud_catalog_auto_refresh_interval_secs":
        "TTL between background catalog refreshes. Rapid relaunches "
        "inside this window no-op so we don't hammer OpenRouter. "
        "Default 3600 = once an hour. `cloud --refresh-catalog` "
        "always bypasses the TTL.",
    "proprietary_models_enabled":
        "FOSS-first gate. False (default) hides closed-API models "
        "(Claude, GPT, Gemini) from cloud --tune suggestions, "
        "blocks the proxy's cloud failover from routing to them, "
        "and skips registering the `claude` subcommand. Open-weight "
        "models (Llama, Qwen, DeepSeek, Kimi) are unaffected. Set "
        "true to opt in.",
    "proxy_prompt_cache_enabled":
        "Phase 18 prefix cache. True (default) injects a generous "
        "`keep_alive` on local Ollama requests (KV-cache survives "
        "between turns) AND marks the largest system message with "
        "`cache_control:ephemeral` on Anthropic-shape outbound "
        "(Anthropic discounts cached input ~90%). False disables "
        "both legs.",
    "proxy_prompt_keep_alive":
        "Duration injected as `options.keep_alive` on local Ollama "
        "requests. Accepts `30m` / `1h` / `0` (immediate unload) / "
        "`-1` (forever). User-set keep_alive in the request always "
        "wins.",
    "insights_auto_open":
        "Auto-pop the /insights dialog at launch. False (default) "
        "shows the toast + /insights slash but skips the modal — "
        "useful while card generators still surface captain's-log "
        "self-noise on long-running vaults. True restores the "
        "Phase 16.1 'cards APPEAR ON OPEN' behaviour.",
    "insights_skip_tags":
        "CSV of tag names (additive to the org-llm baseline) the "
        "insight generators should treat as metadata, not topics. "
        "Excluded from new_captures clustering + topic_cluster "
        "counts. e.g. `inbox,wip,daily`. Empty default.",
    "insights_skip_file_patterns":
        "CSV of filename substrings (additive to baseline) the "
        "insight generators should skip entirely. Default baseline "
        "covers captain's-log, llm-history, config tangles, "
        "insights-cache. Add per-vault patterns here.",
    "insights_disabled_generators":
        "CSV of generator NAMES to disable. Names: "
        "new_captures, stale_candidates, topic_cluster, "
        "orphan_growth, doctor_warnings, sensor_attention. Empty "
        "default = all enabled.",
}


def env_var_for(key: str) -> str:
    """Canonical env override name for a config key.

    Convention: every config key has an ORG_LLM_<KEY.upper()> env tap
    that overrides the DB row at read time. This function returns the
    name regardless of whether it's actively set — callers compose it
    with os.environ.get() to check for an override.
    """
    return "ORG_LLM_" + key.upper()


def effective_value(key: str) -> tuple[str, str]:
    """Resolve a config key with env / DB / default precedence.

    Returns (value, source) where source is one of:
      "env"     — overridden by ORG_LLM_<KEY> at runtime
      "config"  — set in the SQLite config table
      "default" — unset, falling back to MODEL_DEFAULTS
      ""        — unknown key (no value)

    Used by `org-llm config` to surface the live picture with the
    env override visible. Don't use this on the hot path — call sites
    that read once should still hit the DB directly via _cfg().
    """
    env_name = env_var_for(key)
    env_val = os.environ.get(env_name)
    if env_val is not None:
        return (env_val, "env")
    try:
        from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if path.exists():
            engine = make_engine(path)
            with Session(engine) as s:
                row = s.get(Config, key)
                if row and row.value:
                    return (row.value, "config")
        from .db import MODEL_DEFAULTS
        if key in MODEL_DEFAULTS:
            return (MODEL_DEFAULTS[key], "default")
    except Exception:
        pass
    return ("", "")


def literate_path() -> Path:
    """Where the literate config file lives. Env override for tests."""
    return Path(os.environ.get("ORG_LLM_LITERATE_CONFIG_PATH")
                  or str(_LITERATE_PATH))


def tangle_dir() -> Path:
    return Path(os.environ.get("ORG_LLM_LITERATE_CONFIG_TANGLE_DIR")
                  or str(_TANGLE_DIR))


@dataclass
class _ConfigEntry:
    key:          str
    value:        str
    default:      str
    description:  str

    @property
    def is_modified(self) -> bool:
        return (self.value or "") != (self.default or "")


def _gather_entries() -> list[_ConfigEntry]:
    """Pull every relevant key from DB + MODEL_DEFAULTS, merged."""
    from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
    from sqlalchemy.orm import Session
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return []
    engine = make_engine(path)
    rows: dict[str, str] = {}
    with Session(engine) as s:
        for r in s.query(Config).all():
            rows[r.key] = r.value or ""
    entries: list[_ConfigEntry] = []
    all_keys = (set(rows) | set(MODEL_DEFAULTS)) - EXCLUDED_KEYS
    for k in sorted(all_keys):
        entries.append(_ConfigEntry(
            key=k,
            value=rows.get(k, MODEL_DEFAULTS.get(k, "")),
            default=MODEL_DEFAULTS.get(k, ""),
            description=KEY_DESCRIPTIONS.get(k, "(no description)"),
        ))
    return entries


def _read_user_knobs() -> list[dict]:
    """Pull the user_theme_knobs JSON list from config, or [] on failure."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        import json as _json
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return []
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, "user_theme_knobs")
            if not row or not row.value:
                return []
            data = _json.loads(row.value)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_user_knobs(knobs: list[dict]) -> bool:
    """Persist the knob list back to the config table. Best-effort."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        import json as _json
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return False
        engine = make_engine(path)
        payload = _json.dumps(knobs)
        with Session(engine) as s:
            row = s.get(Config, "user_theme_knobs")
            if row: row.value = payload
            else:   s.add(Config(key="user_theme_knobs", value=payload))
            s.commit()
        return True
    except Exception:
        return False


def _render_knobs_section(knobs: list[dict]) -> str:
    """Render Knobs section: one * Knob: <name> heading per knob, with
    an Index drawer (default level + meta) and one * subheading per
    message. Each message body lives in a `:tangle` block so the user
    can edit message text directly in org and apply back."""
    if not knobs:
        return (
            "* Theme knobs\n"
            "  (No user-defined knobs yet — built-in trek/commie/queer "
            "dials are configured in the per-key sections above.)\n\n"
            "  Add one with:\n"
            "    [bold]org-llm knob add NAME --llm "
            "--vibe 'description' "
            "--specifics font=X --specifics color=Y[/bold]\n"
            "  or in opencode: [bold]/knob-add NAME description[/bold]\n\n"
        )
    td = tangle_dir()
    lines = ["* Theme knobs\n",
              "  Each `* Knob: NAME` is a user-built knob. Edit the message\n",
              "  bodies in place, then `org-llm config --apply-from-org` to\n",
              "  push edits back. Add a new one with `org-llm knob add ...`.\n\n"]
    for k in knobs:
        name = k.get("name", "?")
        default_level = k.get("default_level", 2)
        keywords = ", ".join(k.get("keywords") or [])
        msgs = k.get("messages") or []
        lines.append(
            f"** Knob: {name}\n"
            f":PROPERTIES:\n"
            f":KNOB:          {name}\n"
            f":DEFAULT_LEVEL: {default_level}\n"
            f":KEYWORDS:      {keywords}\n"
            f":N_MESSAGES:    {len(msgs)}\n"
            f":END:\n\n"
        )
        for i, m in enumerate(msgs):
            text  = m[0] if isinstance(m, list) and len(m) >= 1 else str(m)
            style = m[1] if isinstance(m, list) and len(m) >= 2 else "lcars1"
            lines.append(
                f"*** msg {i+1} [{style}]\n"
                f"#+name: knob-{name}-msg-{i+1}\n"
                f"#+begin_src text :tangle {td}/knob-{name}-{i+1:02d}.txt\n"
                f"{text}\n"
                f"#+end_src\n\n"
            )
    return "".join(lines)


_KNOB_HEADING_RE   = re.compile(r"^\*\* Knob: (\S+)\s*$", re.MULTILINE)
_MSG_HEADING_RE    = re.compile(
    r"^\*\*\* msg \d+ \[([^\]]+)\]\s*$", re.MULTILINE)
_KNOB_LEVEL_RE     = re.compile(r":DEFAULT_LEVEL:\s*(\d+)")
_KNOB_KEYWORDS_RE  = re.compile(r":KEYWORDS:\s*(.*)$", re.MULTILINE)


def _parse_knobs_from_org(text: str) -> list[dict]:
    """Reverse of _render_knobs_section: extract the knob list from
    the literate org. Robust to message edits + reorderings."""
    out: list[dict] = []
    knob_matches = list(_KNOB_HEADING_RE.finditer(text))
    for i, m in enumerate(knob_matches):
        name = m.group(1).strip()
        end = (knob_matches[i + 1].start() if i + 1 < len(knob_matches)
                else len(text))
        slab = text[m.end():end]
        # Parse meta from the PROPERTIES drawer
        lvl_m = _KNOB_LEVEL_RE.search(slab)
        kw_m  = _KNOB_KEYWORDS_RE.search(slab)
        default_level = int(lvl_m.group(1)) if lvl_m else 2
        keywords = [k.strip() for k in (kw_m.group(1) if kw_m else "").split(",") if k.strip()]
        # Parse messages: each *** msg N [style] heading + adjacent block.
        msgs: list[list[str]] = []
        msg_matches = list(_MSG_HEADING_RE.finditer(slab))
        for j, mm in enumerate(msg_matches):
            style = mm.group(1).strip()
            mend = (msg_matches[j + 1].start() if j + 1 < len(msg_matches)
                     else len(slab))
            body = slab[mm.end():mend]
            blk = _BLOCK_RE.search(body)
            if not blk:
                continue
            value = blk.group(1)
            if value.endswith("\n"):
                value = value[:-1]
            msgs.append([value, style])
        if msgs:
            out.append({
                "name":          name,
                "default_level": default_level,
                "keywords":      keywords,
                "messages":      msgs,
            })
    return out


def _render_org(entries: list[_ConfigEntry]) -> str:
    """Build the full org file body from entries.

    Output shape (per entry):

        * <key>
        :PROPERTIES:
        :KEY:         <key>
        :DEFAULT:     <default>
        :MODIFIED:    yes|no
        :DESCRIPTION: <one-line>
        :END:

        #+name: cfg-<key>
        #+begin_src text :tangle ~/.local/share/org-llm/config/<key>
        <value>
        #+end_src
    """
    td = tangle_dir()
    head = (
        "#+title: org-llm — literate config\n"
        "#+filetags: :org-llm:config:noexport:\n"
        "#+startup: showall\n\n"
        "Round-trip mirror of the SQLite `config` table.\n"
        "  • [bold]org-llm config --tangle[/bold]   → write this file from DB\n"
        "  • [bold]org-llm config --apply-from-org[/bold] → write DB from this file\n"
        "  • [bold]org-llm config --diff-org[/bold]  → show what would change\n\n"
        f"Tangled per-key plaintext mirrors land at {td}/<key>.\n"
        "Edit any block's body, then `org-llm config --apply-from-org`\n"
        "to push your changes back to the DB.\n\n"
    )
    body_parts = [head]
    for e in entries:
        # Multi-line / multi-word values stay verbatim — the tangle
        # block grabs everything between begin_src and end_src.
        body_parts.append(
            f"* {e.key}\n"
            f":PROPERTIES:\n"
            f":KEY:         {e.key}\n"
            f":DEFAULT:     {e.default}\n"
            f":MODIFIED:    {'yes' if e.is_modified else 'no'}\n"
            f":DESCRIPTION: {e.description}\n"
            f":END:\n\n"
            f"#+name: cfg-{e.key}\n"
            f"#+begin_src text :tangle {td}/{e.key}\n"
            f"{e.value}\n"
            f"#+end_src\n\n"
        )
    return "".join(body_parts)


_HEADING_RE = re.compile(r"^\* (\S.*)$", re.MULTILINE)
_BLOCK_RE   = re.compile(
    r"#\+begin_src text :tangle [^\n]+\n(.*?)\n#\+end_src",
    re.DOTALL,
)


def _parse_org(text: str) -> dict[str, str]:
    """Reverse of _render_org: extract {key: value} from the org file.

    Robust to user edits: relies on heading + the immediately-following
    src block. Comments / extra text between are tolerated; only the
    src block's contents become the value. Multi-line values preserved.
    """
    out: dict[str, str] = {}
    # Walk headings + the next src block after each.
    headings = list(_HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        key = m.group(1).strip()
        # Look at the slice between this heading and the next.
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        slab = text[m.end():end]
        block = _BLOCK_RE.search(slab)
        if not block:
            continue
        value = block.group(1)
        # Strip the trailing newline we emit at write time.
        if value.endswith("\n"):
            value = value[:-1]
        out[key] = value
    return out


def tangle_db_to_org(*, keys: list[str] | None = None,
                       include_knobs: bool = True,
                       to_path: Path | None = None) -> Path:
    """Render the literate file from the current DB state. Returns
    the path written. Idempotent — safe to call repeatedly.

    Selective:
      - `keys` (None = all allow-listed) restricts the per-key section
        to the named keys + their fuzzy prefix matches. Anything not
        listed is omitted from the rendered output, but the DB still
        owns the canonical value.
      - `include_knobs` (default True) controls whether the user's
        theme-knob section is rendered.
      - `to_path` overrides the default literate_path() — useful for
        focused side-files (e.g. ~/org/org-llm-doctor-config.org).

    Combined output:
      header → one * heading per allow-listed (filtered) config key →
      optional Theme knobs section with one ** Knob heading per
      registered user knob and *** msg N subheadings carrying messages.
    """
    entries = _gather_entries()
    if keys:
        # Allow exact match + prefix glob ("doctor_*" matches anything
        # starting with "doctor_"). Bare prefix match is the common case
        # for "give me all the doctor knobs in one file".
        wanted: set[str] = set()
        for raw in keys:
            raw = raw.strip()
            if not raw:
                continue
            if raw.endswith("*"):
                stem = raw[:-1]
                wanted.update(e.key for e in entries if e.key.startswith(stem))
            else:
                wanted.add(raw)
        entries = [e for e in entries if e.key in wanted]
    knobs = _read_user_knobs() if include_knobs else []
    p = to_path or literate_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _render_org(entries)
    if include_knobs:
        body += _render_knobs_section(knobs)
    p.write_text(body)
    return p


def apply_org_to_db(*, dry_run: bool = False) -> tuple[int, list[tuple[str, str, str]]]:
    """Read the literate file, write changes back to the DB.

    Returns (n_changed, [(key, old, new), ...]). When `dry_run=True`,
    nothing is written; the change list is the diff that *would* apply.

    Two halves:
      • Per-key config blocks (everything except user_theme_knobs).
      • Theme knobs section — parsed back into the JSON list and
        compared by serialised form so cosmetic edits to messages
        register as a single user_theme_knobs change.
    """
    p = literate_path()
    if not p.exists():
        return (0, [])
    text = p.read_text()
    parsed = _parse_org(text)
    parsed_knobs = _parse_knobs_from_org(text)
    changes: list[tuple[str, str, str]] = []
    from .db import DB_PATH, Config, make_engine
    from sqlalchemy.orm import Session
    import json as _json
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return (0, [])
    engine = make_engine(path)
    with Session(engine) as s:
        for key, new_val in parsed.items():
            if key in EXCLUDED_KEYS:
                continue
            row = s.get(Config, key)
            old_val = row.value if row else ""
            if (old_val or "") == (new_val or ""):
                continue
            changes.append((key, old_val or "", new_val))
            if dry_run:
                continue
            if row:
                row.value = new_val
            else:
                s.add(Config(key=key, value=new_val))
        # Knobs round-trip — diff the JSON serialisation so message
        # text edits register as a single user_theme_knobs change.
        if parsed_knobs:
            knob_row = s.get(Config, "user_theme_knobs")
            old_json = (knob_row.value or "[]") if knob_row else "[]"
            new_json = _json.dumps(parsed_knobs, sort_keys=True)
            try:
                old_norm = _json.dumps(_json.loads(old_json), sort_keys=True)
            except Exception:
                old_norm = old_json
            if old_norm != new_json:
                changes.append(("user_theme_knobs",
                                  f"{len(_json.loads(old_json) if old_json else [])} knob(s)",
                                  f"{len(parsed_knobs)} knob(s) (edits applied)"))
                if not dry_run:
                    if knob_row:
                        knob_row.value = new_json
                    else:
                        s.add(Config(key="user_theme_knobs", value=new_json))
        if not dry_run:
            s.commit()
    return (len(changes), changes)


def diff_db_vs_org() -> list[tuple[str, str, str, str]]:
    """Show every difference between DB and the literate file.

    Returns [(key, db_value, org_value, direction), ...] where
    direction is one of: "db-only" (key in DB, not in org file),
    "org-only" (vice versa), "differ" (both sides have different values).
    """
    p = literate_path()
    parsed = _parse_org(p.read_text()) if p.exists() else {}
    from .db import DB_PATH, Config, MODEL_DEFAULTS, make_engine
    from sqlalchemy.orm import Session
    path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
    if not path.exists():
        return []
    engine = make_engine(path)
    with Session(engine) as s:
        db_rows = {r.key: r.value or "" for r in s.query(Config).all()}
    out: list[tuple[str, str, str, str]] = []
    keys = (set(db_rows) | set(parsed) | set(MODEL_DEFAULTS)) - EXCLUDED_KEYS
    for k in sorted(keys):
        db_v  = db_rows.get(k, "")
        org_v = parsed.get(k, "")
        if k not in parsed and k in db_rows:
            out.append((k, db_v, "", "db-only"))
        elif k not in db_rows and k in parsed:
            out.append((k, "", org_v, "org-only"))
        elif (db_v or "") != (org_v or "") and k in parsed and k in db_rows:
            out.append((k, db_v, org_v, "differ"))
    return out


def autosync_enabled() -> bool:
    """Read the config_org_autosync key with safe fallback to False."""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return False
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, "config_org_autosync")
            return (row.value or "").lower() in {"1", "true", "yes", "on"} \
                if row else False
    except Exception:
        return False


def maybe_autosync() -> None:
    """If autosync is enabled, re-tangle the org file. Best-effort."""
    if not autosync_enabled():
        return
    try:
        tangle_db_to_org()
    except Exception:
        pass
