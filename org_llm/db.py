# [[file:../../../org/20260425230731-org_llm.org::*Models (SQLAlchemy — mirrors schema.sql)][Models (SQLAlchemy — mirrors schema.sql):1]]
from __future__ import annotations

from pathlib import Path

import sqlite_vec
from sqlalchemy import (
    Column, Float, ForeignKey, Index, Integer, LargeBinary, Text,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

DB_PATH = Path("~/.local/share/org-llm/org-llm.db").expanduser()


class Base(DeclarativeBase):
    __allow_unmapped__ = True


class File(Base):
    __tablename__ = "files"

    id         = Column(Integer, primary_key=True)
    path       = Column(Text, nullable=False, unique=True)
    indexed_at = Column(Text, nullable=False)
    node_count = Column(Integer, nullable=False, default=0)
    mtime      = Column(Float, nullable=False)

    nodes      = relationship("Node", back_populates="file",
                              cascade="all, delete-orphan")


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        Index("idx_nodes_file",  "file_id"),
        Index("idx_nodes_title", "title"),
        Index("idx_nodes_tags",  "tags"),
    )

    id        = Column(Integer, primary_key=True)
    file_id   = Column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    node_id   = Column(Text, unique=True)
    title     = Column(Text, nullable=False)
    body      = Column(Text, nullable=False, default="")
    # Two-bucket tag provenance:
    #   tags       — source-of-truth tags from the org file. The indexer
    #                rewrites this column on every `index` run. Never
    #                written by the auto-tagger; safe to clobber.
    #   auto_tags  — LLM-generated tags layered on top. The auto-tagger
    #                writes here; the indexer never touches it. Reads
    #                that want "all tags" should use merged_tags() below.
    # This split lets `tag --redo` re-tag previously-auto-tagged nodes
    # without clobbering hand-curated org-file tags.
    tags             = Column(Text, nullable=False, default="")
    auto_tags        = Column(Text, nullable=False, default="")
    auto_tagged_at   = Column(Float)            # epoch seconds; NULL = never
    auto_tagger_model = Column(Text)            # model name at time of tagging
    mtime     = Column(Float, nullable=False)
    embedding = Column(LargeBinary)

    file       = relationship("File", back_populates="nodes")


class History(Base):
    """Event log mirrored to ~/org/org-llm-log.org via the logbook module.

    Every CLI invocation, LLM round-trip, MCP tool call, set_config write,
    and doctor verdict that we choose to log writes ONE row here AND ONE
    org heading to the log file. Two surfaces, one source of truth — see
    org_llm/logbook.py.

    Original schema was just (timestamp, command, query, response). The
    new fields (kind, model, args, duration_ms, outcome) are additive
    so existing rows keep working; the migration in _migrate_in_place
    backfills them on existing DBs.
    """
    __tablename__ = "history"

    id          = Column(Integer, primary_key=True)
    timestamp   = Column(Text, nullable=False)
    command     = Column(Text, nullable=False)
    query       = Column(Text, nullable=False)
    response    = Column(Text, nullable=False)
    # Additive fields — present after the logbook migration.
    kind        = Column(Text)         # cli | llm | mcp | config | doctor
    model       = Column(Text)         # for llm/mcp events; "" otherwise
    args        = Column(Text)         # JSON-encoded args dict
    duration_ms = Column(Integer)
    outcome     = Column(Text)         # ok | error | refused | timeout


class Config(Base):
    __tablename__ = "config"

    key   = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)


class SensorLog(Base):
    """Timeseries of host-system probes — battery / cpu / mem / disk /
    thermal / network / ollama / auto-embedder. Written by
    `org_llm.life_support.record_readings()` on every probe cycle so
    the LLM advice path + dbt analytics can reason over trends.

    Separate from History (which is for events) because metrics scale
    differently — a 10-minute polling loop at 1Hz writes 6000 rows per
    probe; History was tuned for human-readable event volume.

    The `context` column carries a short snapshot of what the user was
    doing in the seconds before each reading (last few History rows
    summarised). This lets the LLM advice path correlate resource
    spikes with the activity that caused them — e.g. "CPU pegged at
    8.0 while `ask --reason deepseek-r1:7b` ran 240s, consider
    routing reasoning to cloud" instead of just "your CPU is high".
    """
    __tablename__ = "sensor_log"

    id         = Column(Integer, primary_key=True)
    ts         = Column(Integer, nullable=False)   # unix epoch seconds
    probe      = Column(Text,    nullable=False)   # battery / cpu / mem / ...
    value      = Column(Text)                       # raw measurement, str-cast
    normalized = Column(Text)                       # 0.0..1.0 as text (sqlite friendly)
    status     = Column(Text)                       # nominal / watch / alert / critical
    label      = Column(Text)                       # display label snapshot
    message    = Column(Text)                       # trek-themed message snapshot
    context    = Column(Text)                       # last-N History rows summary


class InsightEngagement(Base):
    """User reactions to Phase 12 insight cards.

    Phase 12.5: every card the user marks as bad / clicks / dismisses
    writes a row here. `doctor --diagnose-cards` clusters the rows by
    (card_kind, narration_model) and surfaces patterns:
        - "All 4 bad cards this week were stale_candidates →
           contradiction-detection threshold may be too lax"
        - "Cloud-narrated cards have 80% bad-rate vs local at 20% →
           cloud prompt may have drifted"
    The table is the substrate for that diagnosis. dbt (Phase 12.7)
    will eventually surface a per-generator quality view from it.
    """
    __tablename__ = "insight_engagement"

    id              = Column(Integer, primary_key=True)
    shown_at        = Column(Integer, nullable=False)   # unix epoch seconds
    card_kind       = Column(Text,    nullable=False)   # "new_captures", "stale_candidates", ...
    card_title      = Column(Text)                       # what the user actually saw
    card_body       = Column(Text)                       # the LLM-narrated body (or determ.)
    evidence_json   = Column(Text)                       # raw deterministic anchor (JSON)
    reaction        = Column(Text,    nullable=False, default="")  # "" | "good" | "bad" | "clicked"
    reason          = Column(Text)                       # optional free-form (why was it bad?)
    suggested_cmd   = Column(Text)                       # the / handle we offered
    narration_model = Column(Text)                       # "deterministic" | model name


def _load_sqlite_vec(dbapi_conn, _):
    dbapi_conn.enable_load_extension(True)
    sqlite_vec.load(dbapi_conn)
    dbapi_conn.enable_load_extension(False)
    dbapi_conn.execute("PRAGMA foreign_keys = ON")


def make_engine(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=False)
    event.listen(engine, "connect", _load_sqlite_vec)
    _migrate_in_place(engine)
    return engine


def _migrate_in_place(engine) -> None:
    """Apply additive schema migrations on existing DBs.

    SQLAlchemy's create_all() adds tables but never columns. New columns
    introduced over time (auto_tags / auto_tagged_at / auto_tagger_model)
    must be added here so old DBs keep working without a manual reindex.
    Safe to call on a freshly-created DB — the existence checks no-op.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if not insp.has_table("nodes"):
        return  # init_db hasn't run yet — create_all will add everything.
    # New TABLES introduced over time (sensor_log etc.) need to be
    # created on existing DBs that pre-date them. create_all() is
    # idempotent — it skips tables that already exist — so calling it
    # here is safe and gives us additive table support for free.
    try:
        Base.metadata.create_all(engine)
    except Exception:
        pass
    cols = {c["name"] for c in insp.get_columns("nodes")}
    additions: list[str] = []
    if "auto_tags" not in cols:
        additions.append(
            "ALTER TABLE nodes ADD COLUMN auto_tags TEXT NOT NULL DEFAULT ''"
        )
    if "auto_tagged_at" not in cols:
        additions.append("ALTER TABLE nodes ADD COLUMN auto_tagged_at REAL")
    if "auto_tagger_model" not in cols:
        additions.append("ALTER TABLE nodes ADD COLUMN auto_tagger_model TEXT")
    # ── History table — logbook columns (kind/model/args/duration/outcome).
    # Additive so old rows keep working; new writes via logbook fill them in.
    if insp.has_table("history"):
        h_cols = {c["name"] for c in insp.get_columns("history")}
        for col, decl in (
            ("kind",        "ALTER TABLE history ADD COLUMN kind TEXT"),
            ("model",       "ALTER TABLE history ADD COLUMN model TEXT"),
            ("args",        "ALTER TABLE history ADD COLUMN args TEXT"),
            ("duration_ms", "ALTER TABLE history ADD COLUMN duration_ms INTEGER"),
            ("outcome",     "ALTER TABLE history ADD COLUMN outcome TEXT"),
        ):
            if col not in h_cols:
                additions.append(decl)
    # ── sensor_log table — life-support timeseries.
    # `context` carries the user's recent activity at probe time so
    # the LLM advice path can correlate resource spikes with what
    # caused them. Added after the initial sensor_log shipped, so
    # existing DBs may have the table without the column.
    if insp.has_table("sensor_log"):
        sl_cols = {c["name"] for c in insp.get_columns("sensor_log")}
        if "context" not in sl_cols:
            additions.append(
                "ALTER TABLE sensor_log ADD COLUMN context TEXT"
            )
    if not additions:
        return
    with engine.begin() as conn:
        for stmt in additions:
            conn.execute(text(stmt))


def merged_tags(node) -> str:
    """Union of file-source tags and LLM auto-tags as a space-joined string.

    Order: file tags first (preserves user's authored order), then any
    auto-tags not already present. Use this anywhere a "what tags does
    this node have" question is asked from Python — for SQL filters,
    use `merged_tags_sql()` so the DB does the union itself.
    """
    file_tags = (getattr(node, "tags", "") or "").split()
    auto      = (getattr(node, "auto_tags", "") or "").split()
    seen = set()
    out: list[str] = []
    for t in file_tags + auto:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def merged_tags_sql(alias: str = "n") -> str:
    """SQL fragment for the merged tag string of a row aliased as `alias`.

    Use in raw SQL: `WHERE lower(<merged>) LIKE :pat`. The COALESCE guards
    rows from before the auto_tags migration ran (where auto_tags is ''
    by DEFAULT, but defensive doesn't hurt).
    """
    return f"({alias}.tags || ' ' || COALESCE({alias}.auto_tags, ''))"


MODEL_DEFAULTS = {
    "org_dir":        "~/org",
    "ollama_url":     "http://localhost:11434",
    # Defaults are tuned to fit a laptop CPU/16 GB RAM out of the box. Use
    # `org-llm models --tune` once you've got real hardware to scale up.
    "embed_model":    "nomic-embed-text",   # 137 MB — semantic search
    "chat_model":     "llama3.2",           # 2.0 GB — ask / Q&A (first-flight friendly)
    "code_model":     "qwen2.5-coder",      # 4.7 GB — code generation
    "reason_model":   "deepseek-r1:7b",     # 4.7 GB — planning (full deepseek-r1 is 40 GB)
    "fast_model":     "phi3.5",             # 2.2 GB — tagging, classification
    "instruct_model": "mistral-nemo",       # 7.1 GB — capture, instruction following
    "text_model":     "gemma3",             # 5.4 GB — summarization, text analysis
    "embed_dim":      "768",
    "theme":          "dark",               # dark | light  (UI color mode)
    # ── LCARS palette + per-channel overrides ─────────────────────────────
    # `lcars_palette` picks one of the named bundles in palettes.py
    # (classic | red | green | gold | violet). The per-channel keys
    # accept any hex color and stack on top of the named palette;
    # empty string = no override.
    "lcars_palette":           "classic",
    "lcars_color_primary":     "",
    "lcars_color_secondary":   "",
    "lcars_color_tertiary":    "",
    # ── Theme cross-reference intensity ───────────────────────────────────
    # 0 = no overlap (each knob voiced separately)
    # 1 = sparse (occasional overlaps when 2+ knobs at level 3)
    # 2 = normal (~half of variants bridge worlds when 2+ knobs > 1)
    # 3 = max (every variant finds real overlap between every active knob)
    # See theme_studio._CROSS_REF_GUIDANCE for the exact prompt language.
    "theme_cross_references_level": "2",
    "code_dirs":      "~/repos",            # comma-sep paths for `code-index`
    "db_version":     "1",
    # ── Proactive doctor knobs ─────────────────────────────────────────────
    # `doctor_proactive_mode` — how aggressive the in-opencode LLM should be
    #   off:        never auto-call proactive_doctor; user must invoke
    #               /proactive-doctor explicitly.
    #   passive:    only when the user asks "what's wrong" or similar.
    #   active:     after `doctor_stuck_threshold` tool calls without
    #               convergence, OR on the first hard error. (default)
    #   aggressive: after the FIRST sub-optimal turn (vague reply, empty
    #               search, slow response). Cheap when the LLM is fast,
    #               annoying when it isn't.
    "doctor_proactive_mode":   "active",
    # `doctor_stuck_threshold` — N tool calls before the LLM should
    # consider itself stuck and call proactive_doctor.
    "doctor_stuck_threshold":  "3",
    # `doctor_intervene_in` — comma-sep list of operation classes the
    # LLM should be ready to self-doctor on. Empty list disables every
    # interception path. Recognised tokens:
    #   search-empty       — search_notes / ask_notes returned no hits
    #   tool-error         — any MCP tool returned an error string
    #   long-response      — chat call took >30s (caller-tracked)
    #   vague-reply        — about-to-emit a hedge ("I'm not sure", etc)
    #   optimization       — current setup works but a faster/better one
    #                         is available (bigger fitting model, cloud
    #                         configured but unused, etc.)
    #   stale-index        — vault has files newer than last index run
    #   stale-embeddings   — indexed nodes without embeddings accumulating
    "doctor_intervene_in":     "search-empty,tool-error,long-response,optimization",
    # `doctor_auto_apply` — if true, `org-llm doctor --power-boost` and
    # the proactive_doctor recommendations apply changes WITHOUT a
    # separate --apply flag. Off by default for safety.
    "doctor_auto_apply":       "false",
    # ── Logbook (org_llm/logbook.py) ──────────────────────────────────────
    # `log_level` — overall verbosity gate.
    #   off:     disable all event logging.
    #   minimal: log CLI invocations + doctor verdicts only.
    #   normal:  + LLM calls metadata + MCP tool calls (no full responses).
    #   verbose: + full LLM/MCP request + response bodies.
    "log_level":               "normal",
    # `log_kinds` — comma-sep filter on event kinds. Empty = no kinds.
    # Recognised: cli, llm, mcp, config, doctor, dbt.
    "log_kinds":               "cli,llm,mcp,config,doctor,dbt",
    # `log_max_rows_per_kind` — keep at most N rows per kind in the
    # History table; older entries get pruned at write time. Org file
    # is also rotated when it crosses 5MB.
    "log_max_rows_per_kind":   "1000",
    # `log_auto_reflect_every` — every Nth CLI invocation, run an
    # LLM reflection on Captain's Log and surface a one-line HEADLINE
    # to the user. 0 = disabled. Reflection runs in-band but is time-
    # boxed; failures are silent.
    "log_auto_reflect_every":  "50",
    # ── Background auto-embedder (org_llm/auto_embedder.py) ───────────────
    # `auto_embed_enabled` — daemon thread polls org_dir mtimes and
    # runs incremental index + embed when changes appear. Off by
    # default — opt-in either via this config row or by passing
    # --auto-embed to `org-llm launch`.
    "auto_embed_enabled":      "false",
    # Poll interval (clamped to ≥15s in code).
    "auto_embed_interval_secs": "60",
    # Suppress per-batch terminal output (Captain's Log still records).
    "auto_embed_quiet":        "true",
    # ── Literate config (org_llm/literate_config.py) ──────────────────────
    # Re-tangle ~/org/org-llm-config.org on every set_config write. Off
    # by default so command-line tweaks don't surprise-touch a file the
    # user may not have created yet. Run `org-llm config --tangle` once
    # to seed it; flip this to true to keep it auto-fresh thereafter.
    "config_org_autosync":     "false",
    # ── opencode TUI sidebar panel (Phase 17.1) ──────────────────────────
    # The org-llm sidebar panel renders an LCARS / TNG-styled status
    # readout (vault counts, palette + knobs, MCP, hardware, alerts,
    # 7d activity, top tags, jump links) inside opencode. It mounts in
    # `sidebar_content` (session view) and optionally `home_bottom`
    # (welcome view). Each knob below is read at launch time, written
    # into .opencode/sidebar-status.json's `config` block, and applied
    # by the TypeScript plugin.
    "sidebar_panel_enabled":         "true",
    "sidebar_panel_on_home":         "true",
    "sidebar_panel_on_session":      "true",
    # Section order + selection. Recognised tokens: vault, active,
    # health, model, subsystems, life-support, archive, engage.
    # Default (17.1m) is the consolidated layout: 5 cards instead of
    # 7, fits on most terminal heights without scrolling. The
    # `active` card now includes model/route info (was a separate
    # `model` card), and `health` combines `subsystems` + vitals
    # (was separate `subsystems` and `life-support` cards). The
    # legacy `model` / `subsystems` / `life-support` tokens still
    # work for users with custom configs.
    "sidebar_sections":              "vault,active,health,archive,engage",
    # Refresh tick in seconds. TS clamps to ≥5s — the file is on
    # local disk so polling is cheap, but more often than 5s is wasted.
    "sidebar_refresh_secs":          "15",
    # Width (cols) of the home_bottom panel — sidebar_content uses the
    # opencode sidebar's natural width. 36 fits two columns of label
    # + small value comfortably without overflowing narrow terminals.
    "sidebar_panel_width":           "36",
    # Comma-sep list of opencode internal sidebar plugin IDs (without
    # the "internal:" prefix) to deactivate so our LCARS panel owns
    # the surface. Empty string = keep them all (our panel will append
    # below them, which can cause clutter). Default omits sidebar-context
    # because it carries token usage + session cost we don't surface.
    "sidebar_replace_internal":      "sidebar-mcp,sidebar-lsp,sidebar-todo,sidebar-files",
    # Data-window tunings — applied at generation time in cli.py so the
    # JSON file already reflects the user's preferred windows.
    "sidebar_top_tags_count":        "3",
    "sidebar_activity_window_days":  "7",
    "sidebar_alert_window_hours":    "6",
    "sidebar_alert_limit":           "3",
    # Cosmetic toggles. The "MAKE IT SO" footer and "STARDATE 8xxxx.x"
    # header are TNG flavor; some users want the data without the
    # Trek garnish. 17.1m: make_it_so default flipped to false to
    # save vertical space — set true if you want the footer back.
    "sidebar_make_it_so":            "false",
    "sidebar_stardate_show":         "true",
    # ── Auto-session (Phase 17.1e) ───────────────────────────────────────
    # The full LCARS sidebar lives in opencode's `sidebar_content` slot,
    # which only renders inside the session route — not on the welcome
    # screen. To get the sidebar visible immediately on launch (instead
    # of after the user manually types something), the plugin can
    # programmatically populate + submit a minimal opening prompt at
    # mount time. opencode handles session creation and route navigation
    # in response, and the sidebar comes up.
    "sidebar_auto_session":          "true",
    # Text submitted as the opening prompt. Empty (default) means
    # cli.py renders a dynamic LCARS banner at launch time — small
    # ASCII pill containing ORG-LLM identity, live stardate, vault
    # stats, plus a Trek-themed question line. The banner persists
    # in the chat history (it's the user's first message) so the
    # org-llm identity survives the welcome→session transition.
    # Set this to any literal string to override; verbatim, no
    # templating happens.
    "sidebar_auto_session_prompt":   "",
    # Delay (ms) between plugin mount and auto-submit. With the
    # 17.1f visibility gate (slots.tsx hides home_prompt during the
    # auto-session window), a longer delay is no longer confusing —
    # the user sees the welcome screen + big LCARS logo cleanly
    # without a prompt flashing. 1500ms is a comfortable reveal.
    # Set to 0 for instant submit if you want to skip the reveal.
    "sidebar_auto_session_delay_ms": "1500",
    # Local Ollama model used when the launch is forced into local
    # mode (i.e. auto-session is on AND auto_session_use_cloud is
    # false). Default is a known tool-capable model — your normal
    # `chat_model` may be a text-only one like gemma3 (which Ollama
    # rejects with "does not support tools" when MCP tries to call
    # search_notes, etc.). Setting this guarantees the launched
    # session can use MCP tools. Ignored when not forcing local.
    "sidebar_auto_session_local_model": "llama3.2",
    # Whether the auto-session is allowed to use cloud LLMs. False
    # means: even if cloud is otherwise configured for this launch,
    # the chat session is forced to local Ollama for the auto-prompt
    # (and for the rest of the session — opencode doesn't switch
    # models mid-session). The user can manually swap to cloud after
    # via the agent picker if they want. Default false: don't fire
    # cloud calls on launch unless the user opts in.
    "sidebar_auto_session_use_cloud": "false",
    # Slow-LLM watcher threshold (Phase 17.1j). When the user
    # submits a message and the AI hasn't started responding within
    # this many milliseconds, the plugin auto-runs the
    # proactive_doctor diagnostic and surfaces its findings (RAM
    # vs model fit, cloud routing suggestion, etc.) in a dialog.
    # Set to 0 to disable the watcher entirely. The system-prompt
    # proactive_doctor instructions don't help when the LLM is
    # stuck mid-response; this watches from OUTSIDE the loop.
    "sidebar_slow_llm_threshold_ms":  "25000",
    # LCARS prompt sigil — folded into the top-left CORNER of the
    # rounded prompt box (replaces the `╭` glyph). Empty string
    # disables (corner falls back to plain rounded `╭`). The
    # `customBorderChars` slot is one cell wide, so the FIRST
    # grapheme of this string wins; trailing chars are visual
    # padding for the legacy sibling-text layout (no longer used
    # but kept so existing configs are stable).
    #
    # IMPORTANT: only single-cell-width chars work cleanly. East
    # Asian Width "Ambiguous" chars (▶ ★) render as 2 cells in
    # many terminals but opentui counts them as 1 cell, so the
    # top border drifts 1 column right and visibly bumps into
    # adjacent panels. Use Neutral-width chars:
    #   "❯ " (default, heavy chevron — narrow, terminal-y, LCARS-ish)
    #   "› " (single angle quote — narrowest)
    #   "✦ " (four-pointed star — Neutral)
    # Avoid: "▶ ", "★ ", "◆ ", "◉ " (Ambiguous).
    "sidebar_prompt_char":            "❯ ",
    # Auto-relaunch after the slow-LLM watcher applies the cloud
    # routing fix (Phase 17.1o). When true, the plugin spawns
    # `org-llm launch` as a detached subprocess (3s delay) and
    # exits the current opencode — no manual "Ctrl+C twice + run
    # again" step. The new launch reads the stashed
    # pending-prompt.txt and auto-resubmits the stuck prompt via
    # cloud (fast). Set false to keep the manual relaunch flow.
    "sidebar_slow_llm_auto_relaunch": "true",
    # Inject $ARGS into existing .md slash command bodies on
    # launch (Phase 17.1s-iter17). opencode's project slashes
    # only forward user-typed arguments (e.g. --no-llm) to the
    # LLM if their .md body references the `$ARGS` placeholder.
    # Most user-authored .md files are static prose with no
    # $ARGS. This knob enables a launch-time pass that appends
    # `$ARGS` to every .md in .opencode/command/ that doesn't
    # already contain it — so flags like `--no-llm` reach the
    # proxy and get intercepted.
    #
    # Off by default since it MUTATES USER FILES. Set true and
    # relaunch once when you want the behaviour; can leave on
    # safely (idempotent — only appends when not present).
    "inject_args_in_slashes":         "false",
    # Proxy local-only mode (Phase 17.1s-iter17). When true, the
    # llm_proxy short-circuits EVERY chat completion that wasn't
    # already handled by an upstream interceptor (sys/menu/help/
    # config/cache/--no-llm). The user gets a "no-LLM mode active"
    # response instead of the request hitting ollama. Use case:
    # heavy CPU pressure, thermal throttling, or "I'm not in a
    # state for LLM work right now — just give me my /sys
    # commands and nothing else."
    #
    # Off by default — most users want the LLM to actually work
    # on non-/sys queries. Toggle via `org-llm config
    # proxy_local_only true` to lock the proxy down.
    "proxy_local_only":               "false",
    # ── Cloud failover (Phase 18) ──────────────────────────────────────
    # When the local upstream stops responding before its first byte
    # arrives within `proxy_first_byte_timeout_ms`, the proxy silently
    # retries the chat-completions request against the configured
    # cloud provider for THIS request only. Single retry; the user's
    # next message goes through the local model again. Pairs with
    # `slow-llm-watch.tsx` — that one nudges the user toward a
    # session-wide cloud switch; this one is in-flight cover for the
    # one stuck turn.
    #
    # Master switch. False keeps the existing behaviour where a
    # stalled local upstream surfaces as a 502 to opencode.
    "proxy_cloud_failover_enabled":   "true",
    # Time-to-first-byte threshold for chat completions (ms). Once
    # the local upstream emits any byte, the socket timeout is
    # extended to 300s so legit slow streaming runs through. Tune
    # against your `proxy_first_byte_timeout_ms` audit (see
    # scripts/audit_phase17.py p95). 0 disables failover entirely
    # — equivalent to setting `proxy_cloud_failover_enabled=false`
    # but kept distinct so the kill-switch's "off" isn't confused
    # with a misconfigured threshold of 0.
    "proxy_first_byte_timeout_ms":    "8000",
    # ── Cloud catalog auto-refresh (Phase 18) ──────────────────────────
    # Every `org-llm launch` checks for new cloud models in the
    # background. Without this the bundled catalog drifts behind
    # OpenRouter's live roster — by the time a new flagship model
    # ships (Kimi K3, Claude 5, …), the user has no idea it exists
    # because their catalog was frozen at install time. The refresh
    # is fire-and-forget on a daemon thread; results land in
    # ~/.local/share/org-llm/catalog-last-refresh.json + the
    # captain's log. Network failures never surface to the user.
    # Set false for offline / pinned-catalog workflows.
    "cloud_catalog_auto_refresh_enabled":      "true",
    # TTL between background refreshes (seconds). Default 3600 = 1h.
    # Rapid relaunches inside this window no-op so OpenRouter doesn't
    # see a flood of identical /api/v1/models polls. The user can
    # always force a refresh with `org-llm cloud --refresh-catalog`,
    # which bypasses the TTL.
    "cloud_catalog_auto_refresh_interval_secs": "3600",
    # ── FOSS-first gate (Phase 18) ─────────────────────────────────────
    # org-llm is a FOSS project; the default behaviour treats closed-
    # API models (Claude, GPT, Gemini) as opportunistic — present in
    # the catalog so opt-in is one flag away, but invisible to
    # recommendations, cloud failover, and the `claude` subcommand
    # until the user explicitly enables them. Open-weight models
    # (Llama, Qwen, DeepSeek, Kimi) ARE included by default — those
    # are the licensed-but-redistributable middle ground.
    #
    # Set true to surface proprietary models everywhere they would
    # otherwise be relevant: cloud --tune suggestions, the failover
    # target resolver, the `claude` verb's command registration.
    "proprietary_models_enabled":             "false",
    # ── Prompt-prefix cache (Phase 18) ─────────────────────────────────
    # Two related optimisations rolled into one interceptor
    # (`intercept_prompt_cache` in llm_proxy.py):
    #   • For local Ollama: ensure `options.keep_alive` is set
    #     generously so the model + KV cache survive between turns.
    #     Default ollama keep_alive is 5 minutes; a 30-minute window
    #     covers the typical "user reads response, types follow-up"
    #     gap so the second turn re-uses the prefix's prefilled KV.
    #   • For cloud Claude: mark the largest system-message block
    #     with cache_control={type:"ephemeral"} so Anthropic /
    #     OpenRouter discounts cached input tokens (~90% off when
    #     the same system prompt repeats within 5 minutes). opencode
    #     resends the same 22 KB MCP catalog every turn — that's
    #     exactly the workload prompt caching is designed for.
    #
    # Set false to disable both legs entirely.
    "proxy_prompt_cache_enabled":     "true",
    # keep_alive duration injected when leg 1 fires. Accepts ollama's
    # duration syntax: "30m", "1h", "0" (immediate unload), "-1"
    # (forever). Skipped when the user already set keep_alive in
    # their request — explicit user values always win.
    "proxy_prompt_keep_alive":        "30m",
    # Toast pinning (Phase 17.1s). When true, every toast our
    # plugin emits uses a long duration (1 hour) so messages
    # stay on screen until the user has time to read them. False
    # (default) uses normal short durations (~6s) like vanilla
    # opencode toasts. Per-call overrides via showToast({pin:true})
    # are independent of this knob — those force pinning even
    # when the global knob is off, useful for important state-
    # change notifications (zombie reap, cloud relaunch, etc.).
    "sidebar_pin_toasts":             "false",
    # Chat-injection styling (Phase 17.1r). The plugin emits its
    # /sys* output and auto-doctor proposals as user-role messages
    # (the SDK has no API for assistant-role injection), which
    # opencode displays without markdown rendering. To keep them
    # visually distinct we use:
    #
    #   sidebar_chat_emojis — emoji prefixes in section titles
    #     (🩺 doctor, 💡 proposes, ☁ cloud, 🔄 swap, 🧹 reclaim,
    #     etc.). Disable for a strict-ASCII look or when the
    #     user's terminal font lacks emoji glyphs.
    #   sidebar_chat_frames — box-drawing frames (╭─╮│╰─╯) with
    #     embedded ANSI 24-bit color around each panel. Frames
    #     are LCARS-themed (orange primary, gold accent, peach
    #     secondary, red warning). Disable for a flat layout.
    #
    # Both default true. Either can be flipped independently.
    "sidebar_chat_emojis":            "true",
    "sidebar_chat_frames":            "true",
    # Sidebar scroll keybinds (Phase 17.1r). Each knob is a CSV of
    # `<modifiers>+<key>` bindings the plugin tries in order — the
    # FIRST one your terminal passes through wins, and any of them
    # triggers the scroll. Modifiers: ctrl / alt / shift / meta.
    # Keys: up / down / pageup / pagedown (or any opentui key
    # name). Why per-direction CSV rather than one mega-knob: lets
    # you map weird sequences for ONE direction without rewriting
    # all four (e.g. `ctrl+j` for down only, vim-style).
    #
    # Why these defaults: our experience with doom emacs vterm is
    # that alt+arrow gets eaten as vterm-history; tmux can eat
    # shift+pgup; ctrl+arrow is the most reliable across stacks.
    # The CSV lets us also try the others as fallbacks.
    # Phase 18.4: vim-style ctrl+k/j as the primary bindings — most
    # terminal stacks pass them through cleanly, they don't conflict
    # with opencode's own keymap, and they read naturally to anyone
    # who's used vim/less/man. Arrow-key combos kept as fallbacks
    # for users who prefer arrow navigation; the FIRST binding the
    # user's terminal delivers wins.
    "sidebar_scroll_up_keys":         "ctrl+k,ctrl+up,alt+up,shift+up",
    "sidebar_scroll_down_keys":       "ctrl+j,ctrl+down,alt+down,shift+down",
    "sidebar_scroll_pageup_keys":     "ctrl+u,ctrl+pageup,alt+pageup,shift+pageup",
    "sidebar_scroll_pagedown_keys":   "ctrl+d,ctrl+pagedown,alt+pagedown,shift+pagedown",
    # Confirm-before-act gate for the auto-doctor flow (17.1q).
    # When true (default), the slow-LLM watcher injects the
    # diagnostic + a list of the commands it WILL run, then waits
    # for the user to type `/syscloud` to confirm. When false, it
    # runs the cloud-switch + relaunch immediately as soon as the
    # slow-LLM threshold trips. Default true because relaunching
    # the TUI under the user's feet without a green light is
    # surprising the first time it happens — opt out once you've
    # seen it work and want the fully-autonomous behavior.
    "sidebar_slow_llm_confirm":       "true",
}


def init_db(engine) -> None:
    from org_llm.skills import Skill  # ensure table is registered before create_all
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for k, v in MODEL_DEFAULTS.items():
            if not s.get(Config, k):
                s.add(Config(key=k, value=v))
        s.commit()


def get_session(engine) -> Session:
    return sessionmaker(bind=engine)()
# Models (SQLAlchemy — mirrors schema.sql):1 ends here
