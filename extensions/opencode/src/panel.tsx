// @ts-nocheck
//
// Same JSX-runtime / @ts-nocheck rationale as src/slots.tsx.
//
/**
 * @org-llm/opencode-plugin — shared LCARS / TNG panel module
 * (Phase 17.1).
 *
 * Owns:
 *   • Status JSON cache + loader + refresh tick
 *   • Type definitions for the JSON file (SidebarStatus, SidebarConfig)
 *   • Default config (mirrors MODEL_DEFAULTS in db.py)
 *   • PanelBody + section renderers (vault / active / subsystems /
 *     life-support / archive / engage)
 *
 * Why a shared module: the panel renders in TWO places —
 *   • slots.tsx home_logo  → "sidebar on the welcome screen"
 *     (positioned beside the logo via horizontal flex)
 *   • sidebar.tsx sidebar_content → the in-session sidebar
 * so both modules need the same status cache + the same JSX.
 *
 * Why no solid-js value imports: the original Phase 17 used
 * `import { createSignal } from "solid-js"`. That specifier did NOT
 * resolve in opencode's bun plugin runtime — the whole sidebar
 * module threw at import time, the try/catch in index.ts swallowed
 * it, and the user only saw a 4s toast on launch. 17.1 keeps the
 * cache as a closed-over plain JS variable + setInterval, which
 * works without the solid-js value runtime.
 */

// ── Types ───────────────────────────────────────────────────────────

export interface VaultStats {
  n_files?: number;
  n_nodes?: number;
  n_embedded?: number;
  pct_embedded?: number;
  org_dir?: string;
}
export interface KnobLevel { name: string; level: number }
export interface SensorAlert {
  ts: number; probe: string; status: string; message: string;
}
export interface Vital {
  name: string;       // cpu | memory | disk | thermal
  label: string;      // "load 0.85 / 8c", "11.2 / 16.0 GB free", "240 / 512 GB", "42°C"
  status: string;     // nominal | watch | alert | critical
  norm: number;       // 0..1 (1 = nominal)
}
export interface FeatureLink {
  name: string; title: string; slash: string; hint: string;
  // CLI equivalent of the slash command, surfaced as a second row
  // under the slash so users learn the CLI by seeing it next to
  // the TUI affordance. Empty when the slash is plugin-only and
  // has no clean CLI mapping (e.g. /wiki).
  cli?: string;
}

export interface ModelInfo {
  active?: string;
  provider?: string;
  route?: "local" | "cloud" | string;
  endpoint?: string;
}
export interface ActivityCounts {
  nodes?: number; files?: number; window_days?: number;
}
export interface TopTag { name: string; count: number }

export interface SidebarConfig {
  panel_enabled?: boolean;
  panel_on_home?: boolean;
  panel_on_session?: boolean;
  sections?: string[];
  refresh_secs?: number;
  panel_width?: number;
  replace_internal?: string[];
  make_it_so?: boolean;
  stardate_show?: boolean;
  // Auto-session (Phase 17.1e). The plugin programmatically submits
  // an opening prompt at launch so the sidebar (which only renders
  // in the session route) appears immediately. `auto_session_use_cloud`
  // is enforced launch-side in cli.py — when false, the launch's
  // entire session is forced to local Ollama regardless of cloud
  // config. Documented here for completeness; the TS plugin doesn't
  // act on it directly.
  auto_session?: boolean;
  auto_session_prompt?: string;
  auto_session_use_cloud?: boolean;
  auto_session_delay_ms?: number;
  // Slow-LLM watcher threshold (Phase 17.1j). 0 disables.
  slow_llm_threshold_ms?: number;
  // Trek prompt prefix character (Phase 17.1n). Rendered before
  // the home prompt input. Empty string disables.
  prompt_char?: string;
  // Auto-relaunch after slow-LLM watcher applies cloud fix
  // (Phase 17.1o).
  slow_llm_auto_relaunch?: boolean;
  // Confirm-before-act gate (Phase 17.1q). When true, the slow-LLM
  // watcher diagnoses but waits for user confirmation (`/syscloud`)
  // before applying the cloud-switch + relaunch. False = fully
  // autonomous (relaunch fires immediately on threshold trip).
  slow_llm_confirm?: boolean;
  // Chat-injection styling (Phase 17.1r).
  chat_emojis?: boolean;
  chat_frames?: boolean;
  // Toast pinning (Phase 17.1s).
  pin_toasts?: boolean;
  // Sidebar scroll keybinds (Phase 17.1r). CSV of `<mods>+<key>`
  // bindings per direction. Plugin parses + listens for any of
  // them. Configurable because no single binding works across all
  // terminal stacks (doom emacs vterm eats alt+arrow as
  // vterm-history; tmux can eat shift+pgup).
  scroll_up_keys?: string;
  scroll_down_keys?: string;
  scroll_pageup_keys?: string;
  scroll_pagedown_keys?: string;
  // Phase 17.1l: pending prompt preservation across cloud-fix
  // relaunch. cli.py reads ~/.local/share/org-llm/pending-prompt.txt
  // (written by slow-llm-watch when it auto-applies cloud routing)
  // and surfaces its content here. The auto-session resubmits this
  // text after navigating to the new session — LLM responds
  // normally via cloud. Empty string = no pending restore.
  auto_session_pending_prompt?: string;
}

export interface SidebarStatus {
  generated_at?: number;
  workspace?: string;
  version?: string;
  stardate?: number;
  vault?: VaultStats;
  active?: { palette?: string; knobs?: KnobLevel[] };
  mcp?: { server?: string; tool_count?: number; configured?: boolean };
  model?: ModelInfo;
  hardware?: { free_ram_gb?: number; vram_gb?: number | null };
  vitals?: Vital[];
  sensors?: { recent_alerts?: SensorAlert[] };
  activity?: ActivityCounts;
  top_tags?: TopTag[];
  links?: FeatureLink[];
  config?: SidebarConfig;
}

// Defaults are AUTO-GENERATED from org_llm/db.py MODEL_DEFAULTS
// by scripts/sync_sidebar_defaults.py. Run that script after any
// sidebar_* default change in db.py — panel.tsx imports the
// generated values, so there's no second place to update.
//
// `auto_session_pending_prompt` is the one TS-only field (Python
// emits it at runtime from a stash file but doesn't have a static
// default). Spread + override pattern below carries that.
import { SIDEBAR_DEFAULTS } from "./sidebar-defaults.generated";

export const DEFAULT_CONFIG: Required<SidebarConfig> = {
  ...SIDEBAR_DEFAULTS,
  // Ensure the spread casts cleanly to a mutable Required<...>;
  // `as const` on the source forces readonly.
  sections:         [...SIDEBAR_DEFAULTS.sections],
  replace_internal: [...SIDEBAR_DEFAULTS.replace_internal],
  // Runtime-only field, no Python static default — empty string
  // when no pending prompt has been stashed.
  auto_session_pending_prompt: "",
} as Required<SidebarConfig>;

export function resolveConfig(s: SidebarStatus): Required<SidebarConfig> {
  const c = s.config ?? {};
  return {
    panel_enabled:    c.panel_enabled    ?? DEFAULT_CONFIG.panel_enabled,
    panel_on_home:    c.panel_on_home    ?? DEFAULT_CONFIG.panel_on_home,
    panel_on_session: c.panel_on_session ?? DEFAULT_CONFIG.panel_on_session,
    sections:         c.sections         ?? DEFAULT_CONFIG.sections,
    refresh_secs:     c.refresh_secs     ?? DEFAULT_CONFIG.refresh_secs,
    panel_width:      c.panel_width      ?? DEFAULT_CONFIG.panel_width,
    replace_internal: c.replace_internal ?? DEFAULT_CONFIG.replace_internal,
    make_it_so:       c.make_it_so       ?? DEFAULT_CONFIG.make_it_so,
    stardate_show:    c.stardate_show    ?? DEFAULT_CONFIG.stardate_show,
    auto_session:     c.auto_session     ?? DEFAULT_CONFIG.auto_session,
    auto_session_prompt:    c.auto_session_prompt    ?? DEFAULT_CONFIG.auto_session_prompt,
    auto_session_use_cloud: c.auto_session_use_cloud ?? DEFAULT_CONFIG.auto_session_use_cloud,
    auto_session_delay_ms:  c.auto_session_delay_ms  ?? DEFAULT_CONFIG.auto_session_delay_ms,
    slow_llm_threshold_ms:  c.slow_llm_threshold_ms  ?? DEFAULT_CONFIG.slow_llm_threshold_ms,
    auto_session_pending_prompt:
      c.auto_session_pending_prompt ?? DEFAULT_CONFIG.auto_session_pending_prompt,
    prompt_char:            c.prompt_char            ?? DEFAULT_CONFIG.prompt_char,
    slow_llm_auto_relaunch: c.slow_llm_auto_relaunch ?? DEFAULT_CONFIG.slow_llm_auto_relaunch,
    slow_llm_confirm:       c.slow_llm_confirm       ?? DEFAULT_CONFIG.slow_llm_confirm,
    chat_emojis:            c.chat_emojis            ?? DEFAULT_CONFIG.chat_emojis,
    chat_frames:            c.chat_frames            ?? DEFAULT_CONFIG.chat_frames,
    pin_toasts:             c.pin_toasts             ?? DEFAULT_CONFIG.pin_toasts,
    scroll_up_keys:         c.scroll_up_keys         ?? DEFAULT_CONFIG.scroll_up_keys,
    scroll_down_keys:       c.scroll_down_keys       ?? DEFAULT_CONFIG.scroll_down_keys,
    scroll_pageup_keys:     c.scroll_pageup_keys     ?? DEFAULT_CONFIG.scroll_pageup_keys,
    scroll_pagedown_keys:   c.scroll_pagedown_keys   ?? DEFAULT_CONFIG.scroll_pagedown_keys,
  };
}

// ── Shared status cache ─────────────────────────────────────────────
//
// Single source of truth used by both slots.tsx (home_logo render)
// and sidebar.tsx (sidebar_content). sidebar.tsx owns the lifecycle
// (initial load + refresh tick) since it's awaited on plugin mount;
// slots.tsx just reads via getStatus().

let _cachedStatus: SidebarStatus = {};

export function getStatus(): SidebarStatus { return _cachedStatus; }

/** Pinnable toast helper. Wraps `api.ui.toast` with a `pin`
 * option and respects the `sidebar_pin_toasts` config knob.
 *
 * Resolution order (most-specific wins):
 *   1. explicit `duration` on the call → use as-is
 *   2. `pin: true` on the call → 1 hour
 *   3. `pin: false` on the call → normal duration (default 6s)
 *   4. (no `pin` set) AND `sidebar_pin_toasts` config = true → 1 hour
 *   5. otherwise → normal duration
 *
 * Why 1h rather than infinite: opencode's toast UI has no
 * documented "click to dismiss" affordance from a plugin, and
 * dozens of stuck-on-screen toasts would clutter the chat.
 * 1h is "reads at your own pace" without permanent litter.
 *
 * Use this everywhere instead of bare `api.ui.toast()` so the
 * pin knob has reach. */
export interface ToastOpts {
  variant?:  "info" | "success" | "warning" | "error";
  title?:    string;
  message:   string;
  pin?:      boolean;     // explicit override; ignores cfg.pin_toasts
  duration?: number;      // explicit ms; ignores pin + cfg
}

export const TOAST_DURATION_NORMAL = 6_000;
export const TOAST_DURATION_PINNED = 60 * 60 * 1_000;   // 1 hour

export function showToast(api: any, opts: ToastOpts): void {
  const cfg = resolveConfig(getStatus());
  let duration: number;
  if (typeof opts.duration === "number") {
    duration = opts.duration;
  } else if (opts.pin === true) {
    duration = TOAST_DURATION_PINNED;
  } else if (opts.pin === false) {
    duration = TOAST_DURATION_NORMAL;
  } else if (cfg.pin_toasts) {
    duration = TOAST_DURATION_PINNED;
  } else {
    duration = TOAST_DURATION_NORMAL;
  }
  api.ui?.toast?.({
    variant:  opts.variant,
    title:    opts.title,
    message:  opts.message,
    duration,
  });
}

/** Render a compact one-line hint summarising the user's scroll
 * keybind config. Picks the FIRST binding from each direction's
 * CSV (the rest are fallbacks the plugin also listens for, but
 * showing all four full lists would dwarf the sidebar). Skips
 * directions where the CSV is empty.
 *
 * Examples:
 *   "ctrl+up,alt+up", "ctrl+down,alt+down", "ctrl+pageup", "ctrl+pagedown"
 *     → "ctrl+↑↓ ctrl+pgup/dn"
 *   "f5", "f6", "", ""
 *     → "f5/f6"
 *
 * Plain string interpolation rather than JSX so the caller can
 * drop it into a single <text> node — keeps the hint a stable
 * single row regardless of opentui's flex behaviour. */
export function formatScrollKeyHint(
  up: string, down: string, pgup: string, pgdn: string,
): string {
  const first = (csv: string) =>
    (csv ?? "").split(",")[0]?.trim() ?? "";
  const compactArrow = (s: string) =>
    s.replace(/up\b/i, "↑")
     .replace(/down\b/i, "↓")
     .replace(/pageup\b/i, "pgup")
     .replace(/pagedown\b/i, "pgdn")
     .replace(/pgup/i, "pgup")
     .replace(/pgdn/i, "pgdn");
  // Try to collapse "ctrl+↑ / ctrl+↓" into "ctrl+↑↓" when both
  // share the same modifier prefix — feels less verbose in the
  // narrow sidebar column.
  const u = compactArrow(first(up));
  const d = compactArrow(first(down));
  const pu = compactArrow(first(pgup));
  const pd = compactArrow(first(pgdn));
  let lineLR = "";
  if (u && d) {
    const uMod = u.replace(/[↑↓]$/, "");
    const dMod = d.replace(/[↑↓]$/, "");
    lineLR = (uMod && uMod === dMod) ? `${uMod}↑↓` : `${u}/${d}`;
  } else if (u || d) {
    lineLR = u || d;
  }
  let linePg = "";
  if (pu && pd) {
    const puMod = pu.replace(/pg(up|dn)$/, "");
    const pdMod = pd.replace(/pg(up|dn)$/, "");
    linePg = (puMod && puMod === pdMod) ? `${puMod}pgup/dn` : `${pu}/${pd}`;
  } else if (pu || pd) {
    linePg = pu || pd;
  }
  return [lineLR, linePg].filter(Boolean).join(" ");
}

// Sidebar scrollbox ref, captured by PanelBody's scrollbox `ref`
// callback. Exposed via setSidebarScrollRef / scrollSidebar so the
// keybind handler in sidebar.tsx and the /sysup / /sysdown slash
// commands in sys-commands.tsx can drive scroll position without
// knowing about the panel's internal layout. A null check inside
// scrollSidebar handles the case where the panel isn't mounted yet
// (slot hasn't fired) — we silently no-op rather than throw.
let _sidebarScrollRef: any = null;

export function setSidebarScrollRef(ref: any): void {
  _sidebarScrollRef = ref;
}

/** Scroll the sidebar by `delta` rows. Positive = down, negative
 * = up. `unit` semantics:
 *   "step"     — 1 row of movement (small)
 *   "viewport" — ~10 rows of movement (page-scroll feel)
 *
 * Why we don't use opentui's `scrollBy(delta, "step")`: opentui's
 * "step" unit is multiples of `scrollStep`, which defaults to a
 * sizable value (5-10 rows). A "step" delta of 1 would jump
 * 5-10 rows, often past the content end, leaving the viewport
 * showing empty space — which the user reads as "the sidebar
 * blanked". Direct scrollTop manipulation with explicit row
 * counts gives us deterministic, predictable scroll. We also
 * clamp to [0, scrollHeight - viewport] so scrolling past the
 * end is impossible.
 */
export function scrollSidebar(
  delta: number,
  unit: "step" | "viewport" = "step",
  debugReport?: (info: string) => void,
): void {
  const ref = _sidebarScrollRef;
  if (!ref) {
    debugReport?.("scrollSidebar: ref is null (panel not mounted yet)");
    return;
  }
  try {
    const rows = unit === "viewport" ? 10 : 1;
    const rowDelta = delta * rows;
    const before = (typeof ref.scrollTop === "number") ? ref.scrollTop : 0;
    const scrollHeight = (typeof ref.scrollHeight === "number") ? ref.scrollHeight : -1;

    // Earlier iteration set ref.scrollTop directly. opentui
    // ScrollBox has a `_hasManualScroll` flag that's flipped
    // ONLY by going through the official scroll API
    // (scrollBy/scrollTo/scrollbar key events). Direct property
    // assignment skips that, and stickyStart="top" then re-anchors
    // to 0 on the next render — net result: no visible scroll.
    //
    // Going through scrollBy with unit="absolute" treats the
    // delta as exact row count (not multiples of scrollStep)
    // AND flips the manual-scroll flag, so stickyStart respects
    // our position.
    // Poke opentui's private _hasManualScroll flag before writing.
    // Empirically the setter silently rejects writes when this is
    // false (probably so that programmatic state-restore doesn't
    // override user-driven scrolling). Setting it true via the
    // public scrollBy() API also works but scrollBy's units are
    // unreliable (see iter5/iter6). Direct mutation of the private
    // field is sketchy but stable across opentui 0.2.x — and
    // gracefully no-ops if the field is renamed (catch handles it).
    try { ref._hasManualScroll = true; } catch { /* */ }

    const max = scrollHeight >= 0 ? scrollHeight : Number.MAX_SAFE_INTEGER;
    const next = Math.max(0, Math.min(max, before + rowDelta));
    ref.scrollTop = next;
    const used = "scrollTop=";

    const after = (typeof ref.scrollTop === "number") ? ref.scrollTop : before;
    // Probe extra opentui state to help diagnose silent rejection.
    // Field names are best-guesses based on the d.ts; missing
    // fields read as undefined and stringify as "?".
    const vp = ref.viewport?.height ?? ref._viewportHeight ?? "?";
    const cont = ref.content?.height ?? "?";
    const manual = ref._hasManualScroll ?? "?";
    debugReport?.(
      `${used}: ${before} → ${after} (delta=${rowDelta}, ` +
      `scrollHeight=${scrollHeight}, viewport=${vp}, content=${cont}, ` +
      `manual=${manual}, ref.kind=${ref?.constructor?.name ?? "?"})`,
    );
  } catch (e) {
    debugReport?.(`scrollSidebar threw: ${(e as Error)?.message ?? "unknown"}`);
  }
}

export async function loadStatus(directory: string): Promise<SidebarStatus> {
  const path = `${directory}/.opencode/sidebar-status.json`;
  try {
    const file = Bun.file(path);
    if (!(await file.exists())) return {};
    return (await file.json()) as SidebarStatus;
  } catch {
    return {};
  }
}

export async function refreshStatus(directory: string): Promise<SidebarStatus> {
  _cachedStatus = await loadStatus(directory);
  return _cachedStatus;
}

// ── Visual primitives ──────────────────────────────────────────────

export function fmtAge(ts: number | undefined): string {
  if (!ts) return "—";
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (ageSec < 60) return `${ageSec}s`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m`;
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h`;
  return `${Math.floor(ageSec / 86400)}d`;
}

// Pill / knob meter cells — `█` (FULL BLOCK) and `░` (LIGHT SHADE).
// User feedback 2026-05-01: card right edges drifting by a few cols.
// Earlier we used `▰▱` (PARALLELOGRAM marks, U+25B0/B1) which are
// East-Asian-Width "Ambiguous" — some terminals render them as 2
// cells while opentui's layout calculator treats them as 1, shifting
// the right edge by ~10 cells per meter. `█░` are squarely Neutral-
// width and reliably 1-cell across terminals.
function pillMeter(pct: number, width = 10): string {
  const filled = Math.max(0, Math.min(width, Math.round((pct / 100) * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function knobBar(level: number, max = 5): string {
  const lvl = Math.max(0, Math.min(max, level));
  return "█".repeat(lvl) + "░".repeat(max - lvl);
}

function sensorFg(theme: any, status: string): any {
  switch (status) {
    case "critical": return theme.error;
    case "alert":    return theme.warning;
    case "watch":    return theme.info;
    default:         return theme.textMuted;
  }
}

// ── LCARS card primitives ──────────────────────────────────────────
//
// Earlier iterations used a continuous `▎` left column with `─`
// rules between sections. User feedback 2026-05-01: "lcars stuff to
// be smoother, and curve more fully around the logo text. sidebar
// lcars needs to flow more and have more curve."
//
// opentui ships `borderStyle: "rounded"` (corners ╭ ╮ ╰ ╯) natively.
// Each section now renders as its own rounded card with its title
// baked into the top border (`╭─ VAULT ─────────╮`). The color
// cycle (primary→secondary→accent→warning) is applied to each
// card's border, so the panel reads as a stack of LCARS-flavored
// pill panels — orange / purple / blue / gold — instead of the
// previous boxy column.

// Section color cycle — applied to each card's borderColor.
function sectionColors(t: any): any[] {
  return [t.primary, t.secondary, t.accent, t.warning];
}

// SectionCard — open-right rounded LCARS panel.
//
// Title rendered as the FIRST CONTENT ROW (in section color)
// rather than via opentui's `title` prop. Reason: opentui's
// title rendering pads the top rule with the title text +
// minimum trailing dashes, which on an open-right box (no
// closing right corner forcing uniform box width) makes the
// top rule longer than the bottom by the title-padding overshoot
// — that's the persistent STARDATE misalignment. Putting the
// title inside the content area means opentui's top + bottom
// rules are both plain `╭─...` / `╰─...` and equal length,
// driven by the box's natural content width.
//
// `BorderSides[]` ["top", "left", "bottom"] keeps the card open
// on the right — authentically LCARS, panels bleeding off the
// screen edge.
function SectionCard(props: {
  color: any; title: string; children: any;
}) {
  return (
    <box
      border={["top", "left", "bottom"]}
      borderStyle="rounded"
      borderColor={props.color}
      flexDirection="column"
      paddingLeft={1}
      paddingRight={1}
      flexShrink={0}
    >
      <text fg={props.color}>{props.title}</text>
      {props.children}
    </box>
  );
}

// ── Section renderers ──────────────────────────────────────────────
//
// Each section receives its `color` from the parent PanelBody so the
// column cycle is consistent across the panel regardless of which
// sections the user enabled or in what order.

function SectionVault(props: { s: SidebarStatus; t: any; color: any }) {
  const v = props.s.vault ?? {};
  const { t, color } = props;
  return (
    <SectionCard color={color} title="VAULT">
      <box flexDirection="row">
        <text fg={t.textMuted}>embed  </text>
        <text fg={t.accent}>{pillMeter(v.pct_embedded ?? 0)}</text>
        <text fg={t.text}> {v.pct_embedded ?? 0}%</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>nodes  </text>
        <text fg={t.text}>{v.n_nodes ?? "—"}</text>
        <text fg={t.textMuted}> ({v.n_embedded ?? 0} idx)</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>files  </text>
        <text fg={t.text}>{v.n_files ?? "—"}</text>
      </box>
    </SectionCard>
  );
}

function SectionActive(props: { s: SidebarStatus; t: any; color: any }) {
  const a = props.s.active ?? {};
  const m = props.s.model ?? {};
  const { t, color } = props;
  // Truncate cloud model names like "openai/gpt-oss-20b:free" so the
  // row doesn't overflow narrow sidebars. Show last 17 chars +
  // ellipsis prefix to keep the recognisable suffix.
  const modelStr = m.active ?? "";
  const modelDisplay = modelStr.length > 18 ? "…" + modelStr.slice(-17) : modelStr;
  return (
    <SectionCard color={color} title="ACTIVE">
      <box flexDirection="row">
        <text fg={t.textMuted}>palette </text>
        <text fg={t.secondary}>{a.palette ?? "classic"}</text>
      </box>
      {(a.knobs ?? []).slice(0, 2).map((k) => (
        <box flexDirection="row">
          <text fg={t.textMuted}>{k.name.slice(0, 8).padEnd(8)} </text>
          <text fg={t.warning}>{knobBar(k.level)}</text>
        </box>
      ))}
      {modelDisplay && (
        <box flexDirection="row">
          <text fg={t.textMuted}>model   </text>
          <text fg={t.text}>{modelDisplay}</text>
        </box>
      )}
      {m.provider && (
        <box flexDirection="row">
          <text fg={t.textMuted}>via     </text>
          <text fg={t.accent}>{m.provider}</text>
          <text fg={t.textMuted}> · </text>
          <text fg={m.route === "cloud" ? t.warning : t.success}>
            {m.route ?? "—"}
          </text>
        </box>
      )}
    </SectionCard>
  );
}

function SectionModel(props: { s: SidebarStatus; t: any; color: any }) {
  const m = props.s.model ?? {};
  const { t, color } = props;
  // Truncate cloud model names like "openai/gpt-oss-20b:free" so
  // the card body stays tidy at standard sidebar widths.
  const model = m.active ?? "—";
  const modelDisplay = model.length > 18
    ? "…" + model.slice(-17)
    : model;
  return (
    <SectionCard color={color} title="MODEL">
      <box flexDirection="row">
        <text fg={t.textMuted}>active </text>
        <text fg={t.text}>{modelDisplay}</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>via    </text>
        <text fg={t.accent}>{m.provider || "—"}</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>route  </text>
        <text fg={m.route === "cloud" ? t.warning : t.success}>
          {m.route ?? "—"}
        </text>
      </box>
    </SectionCard>
  );
}

function SectionSubsystems(props: { s: SidebarStatus; t: any; color: any }) {
  const m = props.s.mcp ?? {};
  const h = props.s.hardware ?? {};
  const { t, color } = props;
  return (
    <SectionCard color={color} title="SUBSYSTEMS">
      <box flexDirection="row">
        <text fg={t.textMuted}>mcp    </text>
        <text fg={m.configured ? t.success : t.warning}>{"✦ "}</text>
        <text fg={t.text}>{m.tool_count ?? "—"}</text>
        <text fg={t.textMuted}> tools</text>
      </box>
      {h.free_ram_gb != null && (
        <box flexDirection="row">
          <text fg={t.textMuted}>ram    </text>
          <text fg={t.text}>{h.free_ram_gb}</text>
          <text fg={t.textMuted}> gb free</text>
        </box>
      )}
      {h.vram_gb != null && (
        <box flexDirection="row">
          <text fg={t.textMuted}>vram   </text>
          <text fg={t.text}>{h.vram_gb}</text>
          <text fg={t.textMuted}> gb</text>
        </box>
      )}
    </SectionCard>
  );
}

function SectionLifeSupport(props: { s: SidebarStatus; t: any; color: any }) {
  const alerts = props.s.sensors?.recent_alerts ?? [];
  const vitals = props.s.vitals ?? [];
  const { t, color } = props;
  return (
    <SectionCard color={color} title="LIFE SUPPORT">
      {/* Live system vitals — CPU load, memory, disk, thermals.
          Each row gets a status-coloured pill at the front:
          ✦ nominal (green), ◊ watch (info), ▴ alert (warning),
          ▴ critical (red). The label comes verbatim from the
          life_support probe — same wording as `/life-support`. */}
      {vitals.map((v) => (
        <box flexDirection="row">
          <text fg={sensorFg(t, v.status)}>
            {v.status === "nominal" ? "✦ " : v.status === "watch" ? "◊ " : "▴ "}
          </text>
          <text fg={t.textMuted}>{v.name.slice(0, 6).padEnd(7)}</text>
          <text fg={t.text}>{v.label.slice(0, 18)}</text>
        </box>
      ))}
      {/* Recent alert/critical SensorLog rows (last 6h, configurable
          via sidebar_alert_window_hours). Empty in the common
          all-good case — the vitals above already convey health. */}
      {alerts.map((alert) => (
        <box flexDirection="row">
          <text fg={sensorFg(t, alert.status)}>{"▴ "}</text>
          <text fg={t.text}>{alert.probe.slice(0, 9).padEnd(9)}</text>
          <text fg={t.textMuted}>{fmtAge(alert.ts)}</text>
        </box>
      ))}
      {/* Defensive fallback — if no vitals AND no alerts (probes
          all returned None on a system without psutil / sensors),
          show the canonical nominal line so the section isn't empty. */}
      {vitals.length === 0 && alerts.length === 0 && (
        <box flexDirection="row">
          <text fg={t.success}>{"✦ "}</text>
          <text fg={t.textMuted}>all systems nominal</text>
        </box>
      )}
    </SectionCard>
  );
}

function SectionArchive(props: { s: SidebarStatus; t: any; color: any }) {
  const activity = props.s.activity ?? {};
  const topTags = props.s.top_tags ?? [];
  const { t, color } = props;
  const win = activity.window_days ?? 7;
  return (
    <SectionCard color={color} title="ARCHIVE">
      <box flexDirection="row">
        <text fg={t.textMuted}>{win}d nodes </text>
        <text fg={t.text}>{activity.nodes ?? 0}</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>{win}d files </text>
        <text fg={t.text}>{activity.files ?? 0}</text>
      </box>
      {topTags.map((tag) => (
        <box flexDirection="row">
          <text fg={t.textMuted}>#</text>
          <text fg={t.accent}>{tag.name.slice(0, 9).padEnd(9)}</text>
          <text fg={t.textMuted}>{tag.count}</text>
        </box>
      ))}
    </SectionCard>
  );
}

function SectionEngage(props: { s: SidebarStatus; t: any; color: any }) {
  const links = props.s.links ?? [];
  const { t, color } = props;
  // 17.1m: dropped the per-link `$ org-llm <cli>` second row from
  // the default to keep the sidebar dense enough to fit without
  // scrolling. The CLI mapping data is still in the JSON; users
  // who want the dual-row treatment can extend this renderer.
  // 18.4: added a one-line keybinds hint after the link list so
  // ctrl+j/k are discoverable inside the TUI instead of buried in
  // the config file. Chat scrollback uses opencode's native
  // PageUp/PageDown — also called out so users know which surface
  // each keybind drives.
  return (
    <SectionCard color={color} title="ENGAGE">
      {links.map((link) => (
        <box flexDirection="row">
          <text fg={t.primary}>{">"}</text>
          <text fg={t.text}> </text>
          <text fg={t.accent}>{link.slash.padEnd(11)}</text>
          <text fg={t.textMuted}>
            {(link.title.split(" — ")[1] ?? link.title).slice(0, 18)}
          </text>
        </box>
      ))}
      <box flexDirection="row">
        <text fg={t.textMuted}>{"M-↑/↓"}</text>
        <text fg={t.text}> sidebar · </text>
        <text fg={t.textMuted}>{"/sysup /sysdn"}</text>
      </box>
      <box flexDirection="row">
        <text fg={t.textMuted}>{"PgUp/Dn"}</text>
        <text fg={t.text}> chat (opencode)</text>
      </box>
    </SectionCard>
  );
}

// Combined HEALTH section — subsystems + vitals + mcp + ram in
// one card. Default-on in 17.1m to consolidate the sidebar so it
// fits without scrolling on most terminals. The legacy
// "subsystems" and "life-support" tokens still work for users
// with custom config sections lists.
function SectionHealth(props: { s: SidebarStatus; t: any; color: any }) {
  const m = props.s.mcp ?? {};
  const h = props.s.hardware ?? {};
  const vitals = props.s.vitals ?? [];
  const alerts = props.s.sensors?.recent_alerts ?? [];
  const { t, color } = props;
  return (
    <SectionCard color={color} title="HEALTH">
      {vitals.map((v) => (
        <box flexDirection="row">
          <text fg={sensorFg(t, v.status)}>
            {v.status === "nominal" ? "✦ " : v.status === "watch" ? "◊ " : "▴ "}
          </text>
          <text fg={t.textMuted}>{v.name.slice(0, 6).padEnd(7)}</text>
          <text fg={t.text}>{v.label.slice(0, 18)}</text>
        </box>
      ))}
      <box flexDirection="row">
        <text fg={t.textMuted}>  mcp    </text>
        <text fg={m.configured ? t.success : t.warning}>{"✦ "}</text>
        <text fg={t.text}>{m.tool_count ?? "—"}</text>
        <text fg={t.textMuted}> tools</text>
      </box>
      {h.free_ram_gb != null && (
        <box flexDirection="row">
          <text fg={t.textMuted}>  ram    </text>
          <text fg={t.text}>{h.free_ram_gb}</text>
          <text fg={t.textMuted}> gb free</text>
        </box>
      )}
      {alerts.slice(0, 2).map((alert) => (
        <box flexDirection="row">
          <text fg={sensorFg(t, alert.status)}>{"▴ "}</text>
          <text fg={t.text}>{alert.probe.slice(0, 9).padEnd(9)}</text>
          <text fg={t.textMuted}>{fmtAge(alert.ts)}</text>
        </box>
      ))}
    </SectionCard>
  );
}

const SECTION_RENDERERS: Record<string, (p: { s: SidebarStatus; t: any; color: any }) => any> = {
  "vault":        SectionVault,
  "active":       SectionActive,
  "health":       SectionHealth,
  "model":        SectionModel,
  "subsystems":   SectionSubsystems,
  "life-support": SectionLifeSupport,
  "archive":      SectionArchive,
  "engage":       SectionEngage,
};

// ── The panel ──────────────────────────────────────────────────────
//
// Sections render as rounded LCARS cards with their title baked into
// the top border. Each card cycles through the color palette
// (primary→secondary→accent→warning) so the panel reads as a stack
// of variegated pill panels. The stardate header is its own card
// (primary-colored, "STARDATE" labelled) and the make-it-so footer
// is the trailing card. opentui's native `borderStyle="rounded"`
// gives ╭ ╮ ╰ ╯ corners so the curves match real LCARS panels
// rather than the previous block-bar approximation.
export function PanelBody(props: {
  status: SidebarStatus;
  theme: any;
  compact?: boolean;
  terminalHeight?: number;
}) {
  const s = props.status;
  const t = props.theme;
  const cfg = resolveConfig(s);
  const cycle = sectionColors(t);

  // Compute an explicit pixel height for the scrollbox. Without
  // it, opentui's flex layout grows the scrollbox to its content
  // size — opencode then clips externally, but opentui never
  // sees the clip and reports `viewport == content` (no scroll
  // needed). Earlier attempts to fix via `flex: 1 1 0` /
  // `minHeight={0}` collapsed the scrollbox to 1 row instead.
  //
  // Solution: read api.renderer.terminalHeight and budget chrome
  // (chat header + status bar + prompt = ~14 rows). Cap to a
  // sensible minimum so very small terminals still get a usable
  // panel. The result is the height the scrollbox renders at;
  // opentui then knows it has a finite viewport, content larger
  // than that overflows, and scroll positions are real.
  const termH = props.terminalHeight ?? 50;
  const sidebarHeight = Math.max(15, termH - 14);

  // Layout: scrollbox with stickyStart="top" so initial scroll
  // anchors to the top of the content (showing STARDATE header
  // first), but the user can scroll DOWN through overflow content
  // via mouse wheel when the sidebar has hover focus.
  //
  // 17.1p (drop scrollbox): user reported "top cut off". Cause was
  // `stickyScroll=true` defaulting to bottom-pin.
  // 17.1q (plain box): worked but content overflowed off the
  // bottom with no scroll affordance.
  // 17.1q-iter9 (this): scrollbox + stickyStart="top" anchors at
  // top on mount, mouse-wheel scrolls. Footer hint inside the
  // box surfaces the scroll affordance — without it the
  // discoverability is zero.
  return (
    <scrollbox
      flexShrink={1}
      height={sidebarHeight}
      scrollY={true}
      scrollX={false}
      stickyScroll={false}
      ref={(r: any) => setSidebarScrollRef(r)}
    >
      {/* Scroll affordance — slash commands lead because they
              work everywhere (vterm, tmux, screen, kitty…). Keybind
              hint reads from cfg.scroll_*_keys so it always matches
              what the user has actually configured. Empty string
              keys → hide the second line entirely. */}
      <box flexDirection="column">
        <text fg={t.textMuted}>{"↕ /sysup /sysdn"}</text>
        {(cfg.scroll_up_keys || cfg.scroll_down_keys) && (
          <text fg={t.textMuted}>
            {"  " + formatScrollKeyHint(
              cfg.scroll_up_keys, cfg.scroll_down_keys,
              cfg.scroll_pageup_keys, cfg.scroll_pagedown_keys,
            )}
          </text>
        )}
      </box>

      {/* ── Stardate header. Plain ASCII-only text — earlier
              iterations used `✦ STARDATE` but `✦` (U+2726) renders
              wide in some terminals (East Asian Width Neutral
              technically, but font-dependent), pushing the right
              edge past where opentui calculates it should be and
              causing the visible misalignment with cards below.
              Plain "STARDATE" is unambiguous. */}
      {cfg.stardate_show && (
        <box flexDirection="column">
          <text fg={t.primary}>
            {`STARDATE ${s.stardate?.toFixed?.(1) ?? "—"}`}
          </text>
          <text fg={t.textMuted}>
            {`org-llm v${s.version ?? "dev"} · ${s.workspace ?? "all"} · ${fmtAge(s.generated_at)} ago`}
          </text>
        </box>
      )}

      {/* ── Sections in user-configured order; color cycles ────── */}
      {cfg.sections.map((name, i) => {
        const R = SECTION_RENDERERS[name];
        if (!R) return null;
        const color = cycle[i % cycle.length];
        return <R s={s} t={t} color={color} />;
      })}

      {/* ── Footer card — "MAKE IT SO" inside a primary-colored
              rounded box. Off by default in 17.1m to save vertical
              space; users who want it set sidebar_make_it_so=true. */}
      {cfg.make_it_so && (
        <SectionCard color={t.primary} title="">
          <box flexDirection="row" justifyContent="center">
            <text fg={t.primary}>{"✦ "}</text>
            <text fg={t.text}>MAKE IT SO</text>
            <text fg={t.primary}>{" ✦"}</text>
          </box>
        </SectionCard>
      )}
    </scrollbox>
  );
}
