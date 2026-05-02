// @ts-nocheck
//
// Same JSX-runtime/types reconciliation note as src/slots.tsx applies.
//
/**
 * @org-llm/opencode-plugin — Phase 17.1 sidebar + home-status
 * registration.
 *
 * Owns:
 *   • sidebar_content — full LCARS panel (session view)
 *   • home_bottom — one-line status banner (welcome view), visible
 *     immediately on launch without consuming the prompt's vertical
 *     position. (An earlier iteration mounted the full panel beside
 *     the logo via home_logo; that pushed the prompt off-screen
 *     because the panel is ~50 lines tall. The single-line summary
 *     replaces it: still visible on open, doesn't fight the prompt.)
 *   • Lifecycle of the shared status cache (initial load + refresh
 *     tick + dispose handler)
 */

import {
  refreshStatus, getStatus, resolveConfig, PanelBody, fmtAge,
  scrollSidebar, showToast,
} from "./panel";
import { getPromptRef } from "./auto-session";
import { dispatchSysCommand } from "./sys-commands";

// Single-line home status banner. Renders below the prompt via
// the `home_bottom` slot. Pulls live values out of the cached
// sidebar-status.json so it tracks the same data as the in-session
// LCARS panel. Centered, separator-pipe style, height: 1 row.
/** Parsed keybind: a key name plus required modifier state.
 * Modifiers default to false (must match exactly) — so "up"
 * (no modifiers) only fires on bare arrow, not ctrl+up. We
 * may revisit if users want "up matches ANY modifier" semantics,
 * but exact-match is safer (avoids stealing typed text). */
interface ParsedKeybind {
  name:  string;        // opentui key name, lowercase
  ctrl:  boolean;
  alt:   boolean;
  shift: boolean;
  meta:  boolean;
}

/** Parse a CSV of `<modifiers>+<key>` bindings into ParsedKeybind
 * objects. Tolerant: blank tokens drop, malformed tokens drop.
 * Modifier aliases recognised: ctrl/control, alt/meta/option,
 * shift, super/cmd. Key aliases: pgup→pageup, pgdn→pagedown. */
function parseKeybindCSV(csv: string | undefined): ParsedKeybind[] {
  if (!csv) return [];
  const out: ParsedKeybind[] = [];
  for (const token of csv.split(",").map((s) => s.trim()).filter(Boolean)) {
    const parts = token.toLowerCase().split("+").map((p) => p.trim()).filter(Boolean);
    if (parts.length === 0) continue;
    const key = parts.pop()!;
    const bind: ParsedKeybind = {
      name: key === "pgup" ? "pageup" : key === "pgdn" ? "pagedown" : key,
      ctrl: false, alt: false, shift: false, meta: false,
    };
    for (const mod of parts) {
      if (mod === "ctrl" || mod === "control") bind.ctrl = true;
      else if (mod === "alt" || mod === "meta" || mod === "option") bind.alt = true;
      else if (mod === "shift") bind.shift = true;
      else if (mod === "super" || mod === "cmd") bind.meta = true;
    }
    out.push(bind);
  }
  return out;
}

/** Test if an opentui keypress event matches any of the parsed
 * bindings. opentui's KeyEvent exposes `name`, `ctrl`, `option`
 * (alt), `shift`, `meta`. We compare exact modifier state — a
 * binding for "up" only fires on bare arrow, not ctrl+up. The
 * `name` accepts opentui aliases (pagedown sometimes arrives as
 * "pagedn" depending on terminal — handled in
 * parseKeybindCSV's normalisation). */
function matchKeybind(evt: any, binds: ParsedKeybind[]): boolean {
  if (!evt) return false;
  const evtName = (evt.name ?? "").toLowerCase();
  // Normalise opentui's pagedn alias the same way the parser does.
  const name = evtName === "pagedn" ? "pagedown" : evtName;
  const ctrl  = !!evt.ctrl;
  const alt   = !!(evt.option || evt.meta);   // opentui calls alt "option"
  const shift = !!evt.shift;
  // We don't currently distinguish super/cmd separately from "meta"
  // because opentui rolls them together; treat super in user
  // bindings as falsy here (rare in practice).
  for (const b of binds) {
    if (b.name === name && b.ctrl === ctrl && b.alt === alt && b.shift === shift) {
      return true;
    }
  }
  return false;
}

function HomeStatusBanner(props: { theme: any }) {
  const t = props.theme;
  const s = getStatus();
  // If the JSON hasn't loaded yet (no `config` key as marker), don't
  // render anything — avoids a row of "—"s on a fresh checkout.
  if (s.config === undefined) return null;
  const v = s.vault ?? {};
  const m = s.model ?? {};
  return (
    <box flexDirection="row" justifyContent="center" paddingTop={1}>
      <text fg={t.primary}>★ </text>
      <text fg={t.text}>STARDATE {s.stardate?.toFixed?.(1) ?? "—"}</text>
      <text fg={t.textMuted}>  ·  </text>
      <text fg={t.text}>{v.pct_embedded ?? 0}% indexed</text>
      <text fg={t.textMuted}>  ·  </text>
      <text fg={t.accent}>{m.provider || "ollama"}</text>
      <text fg={t.textMuted}>:</text>
      <text fg={t.text}>{(m.active || "—").slice(-18)}</text>
      <text fg={t.textMuted}>  ·  </text>
      <text fg={t.textMuted}>{fmtAge(s.generated_at)} ago</text>
      <text fg={t.primary}> ★</text>
    </box>
  );
}

export async function registerSidebar(api: any): Promise<void> {
  const directory = api.state.path.directory;

  // Read the JSON file once before deciding what to register —
  // panel_enabled, panel_on_session, refresh_secs, and
  // replace_internal all need to be known up front. If the file is
  // missing (first launch / fresh checkout / non-launch invocation)
  // resolveConfig falls through to DEFAULT_CONFIG.
  await refreshStatus(directory);
  const cfg = resolveConfig(getStatus());

  // Master kill switch. When false, no slots register and no
  // internal plugins are deactivated — opencode looks exactly like
  // vanilla opencode (modulo the branding slots in slots.tsx).
  if (!cfg.panel_enabled) return;

  // Intercept-confirmation toaster. Polls the audit JSONL on a
  // tick; for every new line that's an interceptor short-circuit
  // (intercept_sys_commands, intercept_static_slashes), fires a
  // toast confirming "ollama not contacted." This is the direct
  // user-visible signal that the LLM was bypassed — paired with
  // the in-chat "✓ handled locally" marker, double-confirms.
  // Position is tracked across ticks via a closure-captured
  // offset; we only read NEW bytes since last poll.
  void (async () => {
    const home = process.env.HOME ?? "";
    const auditPath = `${home}/.local/share/org-llm/llm-audit.jsonl`;
    let lastPos = 0;
    try {
      const stat = await Bun.file(auditPath).size;
      lastPos = stat ?? 0;   // start at end-of-file → ignore historical entries
    } catch { /* */ }

    // Set of interceptors that fully short-circuited the LLM
    // call. When we see one in the audit log we know ollama
    // wasn't contacted and the auto-doctor stopwatch should
    // disarm. The intercept_md_skills / intercept_no_llm_flag /
    // intercept_local_only are ALSO short-circuits and shouldn't
    // trigger auto-doctor — list them all here.
    const SHORT_CIRCUIT_INTERCEPTORS = new Set<string>([
      "intercept_sys_commands",
      "intercept_static_slashes",
      "intercept_md_skills",
      "intercept_no_llm_flag",
      "intercept_local_only",
      "intercept_response_cache",
      "intercept_probe_cache",
    ]);

    const tickInterval = setInterval(async () => {
      try {
        const file = Bun.file(auditPath);
        const size = file.size;
        if (size <= lastPos) return;
        const slice = file.slice(lastPos, size);
        const text  = await slice.text();
        lastPos = size;
        for (const line of text.split("\n")) {
          if (!line.trim()) continue;
          let entry: any;
          try { entry = JSON.parse(line); } catch { continue; }
          const ib = entry.intercepted_by;
          if (SHORT_CIRCUIT_INTERCEPTORS.has(ib)) {
            // Disarm slow-LLM watcher: this request was handled
            // at the HTTP layer in single-digit ms, so the
            // auto-doctor's 25s timeout should not fire for it.
            // Setting the global module state directly (we can't
            // import slow-llm-watch here without circular dep —
            // instead, use a global symbol the watcher reads).
            (globalThis as any).__orgllm_proxy_recent_intercept_at = Date.now();

            const userText = (entry.user_text || "").slice(0, 60);
            const dur = entry.duration_ms?.toFixed?.(1) ?? "?";
            showToast(api, {
              variant: "success",
              title:   "✓ LLM bypassed",
              message: `${userText} → ${ib.replace("intercept_", "")} in ${dur}ms. Ollama not contacted.`,
            });
          }
        }
      } catch { /* best-effort */ }
    }, 1_500);
    api.lifecycle?.onDispose?.(() => clearInterval(tickInterval));
  })();

  // Zombie-reaper notification. cli.py drops a marker file when
  // it kills orphaned opencode processes from previous launches;
  // surface that as a toast inside the new opencode session so
  // the user sees the cleanup happened (the launch banner scrolls
  // off too quickly). Marker is consumed (deleted) after the
  // toast fires so we don't re-toast on every plugin reload.
  void (async () => {
    try {
      const home = process.env.HOME ?? "";
      const marker = `${home}/.local/share/org-llm/zombies-reaped.json`;
      const file = Bun.file(marker);
      if (!(await file.exists())) return;
      const data = await file.json();
      const count = data?.count ?? 0;
      const pids  = Array.isArray(data?.pids) ? data.pids : [];
      const byKind = data?.by_kind ?? {};
      if (count > 0) {
        // Build a compact breakdown like "2× opencode, 1× org-llm mcp".
        const kindParts: string[] = [];
        for (const [kind, kindPids] of Object.entries(byKind)) {
          const n = Array.isArray(kindPids) ? kindPids.length : 0;
          kindParts.push(`${n}× ${kind}`);
        }
        const breakdown = kindParts.length > 0 ? kindParts.join(", ") : `${count} processes`;
        // Force pin — user wants to verify what got cleaned up;
        // a 6-second toast vanishes before they can read PIDs.
        showToast(api, {
          variant:  "success",
          title:    "Reaped zombies from prior launches",
          message:  `${breakdown}. PIDs: ${pids.join(", ")}.`,
          pin:      true,
        });
      }
      // Consume the marker so subsequent plugin loads in this
      // session don't re-fire the toast.
      try { await Bun.write(marker, ""); } catch { /* */ }
      try { await import("node:fs").then(fs => fs.unlinkSync(marker)); } catch { /* */ }
    } catch {
      // Best-effort — never block plugin init on this.
    }
  })();

  // Refresh tick. Min 5s — local-disk file, faster wastes CPU.
  const tickMs = Math.max(5_000, cfg.refresh_secs * 1_000);
  const tick = setInterval(() => { void refreshStatus(directory); }, tickMs);
  api.lifecycle?.onDispose?.(() => clearInterval(tick));

  // Dynamic re-draw on LLM output (Phase 17.1g). When the user
  // submits a message and the LLM responds, the chat surface
  // re-renders, and opencode invokes our sidebar_content slot
  // function — that gives us a chance to read fresh data. Force
  // a JSON re-read on every message event so the cached status is
  // up-to-date by the time the slot fires.
  //
  // Note: this only matters when `.opencode/sidebar-status.json`
  // is being updated externally — by `org-llm launch` (one-off at
  // startup) or by an auto-embedder daemon (future work). Without
  // an updater, refreshStatus reads the same content repeatedly.
  // Wiring the event subscription now means the live update path
  // is ready when the daemon ships, no plugin change needed.
  const offMsg = api.event?.on?.("message.updated", () => {
    void refreshStatus(directory);
  });
  const offPart = api.event?.on?.("message.part.updated", () => {
    void refreshStatus(directory);
  });
  api.lifecycle?.onDispose?.(() => { offMsg?.(); offPart?.(); });

  // Sidebar scroll keybinds — config-driven. Each direction has a
  // CSV of bindings; we parse once at registration time, then
  // match every keypress against the parsed list. Reliability of
  // any specific combo depends on the terminal stack:
  //   - doom emacs vterm eats alt+arrow as vterm-history
  //   - tmux/screen can eat shift+pgup
  //   - bare terminals usually pass everything through
  // The CSV defaults try ctrl, alt, shift in that order so the
  // first one to reach us wins. Users can override per-direction
  // via sidebar_scroll_*_keys config knobs.
  const upBinds   = parseKeybindCSV(cfg.scroll_up_keys);
  const downBinds = parseKeybindCSV(cfg.scroll_down_keys);
  const pgUpBinds = parseKeybindCSV(cfg.scroll_pageup_keys);
  const pgDnBinds = parseKeybindCSV(cfg.scroll_pagedown_keys);
  const offKey = api.renderer?.keyInput?.on?.("keypress", (evt: any) => {
    // Pre-empt Enter on /sys* messages BEFORE opencode's Prompt
    // processes it. The onSubmit wrapper in slots.tsx isn't a
    // reliable gate — opencode's Prompt fires submission directly
    // on its own Enter handler, not via the onSubmit callback.
    // Hooking keypress with preventDefault + stopPropagation is
    // the only place we can actually stop the LLM round-trip
    // from being initiated. opentui's KeyEvent supports both
    // (see lib/KeyHandler.d.ts).
    const name = (evt?.name ?? "").toLowerCase();
    if ((name === "return" || name === "enter") &&
        !evt?.ctrl && !evt?.option && !evt?.meta && !evt?.shift) {
      const ref = getPromptRef();
      const text = ref?.current?.input ?? "";
      if (text.trim().startsWith("/sys")) {
        if (dispatchSysCommand(api, text)) {
          try { ref?.set?.({ input: "", mode: "normal", parts: [] }); } catch { /* */ }
          evt?.preventDefault?.();
          evt?.stopPropagation?.();
          return;
        }
      }
    }

    // Sidebar scroll keybinds.
    let delta = 0;
    let unit: "step" | "viewport" = "step";
    if      (matchKeybind(evt, upBinds))   { delta = -1; unit = "step"; }
    else if (matchKeybind(evt, downBinds)) { delta = +1; unit = "step"; }
    else if (matchKeybind(evt, pgUpBinds)) { delta = -1; unit = "viewport"; }
    else if (matchKeybind(evt, pgDnBinds)) { delta = +1; unit = "viewport"; }
    else return;
    scrollSidebar(delta, unit);
    evt?.preventDefault?.();
    evt?.stopPropagation?.();
  });
  // Only warn when the API surface IS present but subscription
  // returned falsy — that's a real failure mode worth surfacing.
  // When api.renderer.keyInput is entirely absent (e.g. in unit
  // tests with a mock api), stay silent: that's an expected case,
  // not a bug.
  if (api.renderer?.keyInput?.on && !offKey) {
    showToast(api, {
      variant: "warning",
      title:   "Sidebar scroll keybinds inactive",
      message: "keyInput.on returned null. Use /sysup /sysdown.",
    });
  }
  api.lifecycle?.onDispose?.(() => { offKey?.(); });

  // Deactivate the internal sidebar plugins the user opted to
  // replace. cfg.replace_internal carries IDs WITHOUT the
  // "internal:" prefix (more readable in the literate config csv).
  for (const id of cfg.replace_internal) {
    try {
      void api.plugins?.deactivate?.(`internal:${id}`);
    } catch {
      // Best-effort — non-fatal if the plugin manager rejects.
    }
  }

  // Compose the slot map based on which surfaces the user enabled.
  // Each slot's render is wrapped in safeSlot so a JSX/render bug
  // surfaces as a one-time toast and renders nothing — never
  // crashes the host TUI.
  const slots: Record<string, any> = {};
  if (cfg.panel_on_session) {
    slots.sidebar_content = safeSlot(api, "sidebar_content",
      (ctx: any) => (
        <PanelBody status={getStatus()} theme={ctx.theme.current}
                    terminalHeight={api.renderer?.terminalHeight ?? 50} />
      ));
  }
  if (cfg.panel_on_home) {
    slots.home_bottom = safeSlot(api, "home_bottom",
      (ctx: any) => (
        <HomeStatusBanner theme={ctx.theme.current} />
      ));
  }
  if (Object.keys(slots).length > 0) {
    api.slots.register({ order: 1000, slots });
  }
}

/** Defensive slot wrapper — same pattern as slots.tsx. Inlined
 * here rather than imported to keep sidebar.tsx self-contained
 * and avoid a circular import with slots.tsx. */
function safeSlot<F extends (...args: any[]) => any>(api: any, label: string, fn: F): F {
  let toasted = false;
  return ((...args: any[]) => {
    try {
      return fn(...args);
    } catch (e) {
      if (!toasted) {
        toasted = true;
        try {
          // Force-pin: a slot crash is critical info and the user
          // needs to see the error message to file/fix.
          showToast(api, {
            variant: "error",
            title:   `slot crash: ${label}`,
            message: (e as Error)?.message?.slice(0, 200) ?? "unknown error",
            pin:     true,
          });
        } catch {
          // last-ditch
        }
      }
      return null;
    }
  }) as F;
}
