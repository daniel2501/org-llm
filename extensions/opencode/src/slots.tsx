// @ts-nocheck
//
// JSX-runtime/types reconciliation note (2026-04-29):
//
// tsconfig sets `jsxImportSource: "solid-js"` because @opentui/solid
// 0.2.0 ships only `jsx-runtime.d.ts` (types) — there is NO actual
// `jsx-runtime.js` in the package, so any tsconfig that points
// jsxImportSource at @opentui/solid hits a runtime ImportError when
// bun resolves the JSX factory. solid-js has the real runtime.
//
// BUT solid-js's default JSX.IntrinsicElements is DOM-shaped — `text`
// resolves to `SVGTextElement` and refuses `fg`, `box` is missing,
// etc. Module augmentation (src/jsx.d.ts) merges opentui's intrinsics
// into solid-js's namespace, but interface-merging keeps both
// signatures for conflicting tags (`text` stays as the SVG element).
//
// Runtime is fine — opencode's TUI host wires opentui's Solid renderer
// at TUI launch, so `<box>` and `<text fg={rgba}>` ARE interpreted
// as opentui Renderables when opencode invokes our slot functions.
// Only the TS checker is confused. `@ts-nocheck` is the smallest
// blast-radius fix until @opentui/solid ships a real jsx-runtime.js.
//
/**
 * @org-llm/opencode-plugin — slot overrides (Phase 17.1).
 *
 * Replaces opencode's built-in TUI surfaces with org-llm branding:
 *   • home_logo — colored LCARS bars + multi-tone wordmark, AND
 *     when sidebar_panel_on_home is true, a side-mounted panel
 *     (the home-screen "sidebar" — opencode 1.14.31's home view
 *     has no native sidebar slot, so we co-locate the panel beside
 *     the logo here, where there's vertical room).
 *   • home_prompt — themed placeholder rotation
 *   • sidebar_title — "org-llm •" prefix on session sidebar header
 *
 * Theme handling: Theme key strings ("primary", etc.) are NOT
 * resolved by opentui's color parser — the renderer treats them as
 * literal CSS colors and falls back to default fg when unparseable.
 * Internal opencode plugins read RGBA from `ctx.theme.current.<key>`
 * and pass that to the JSX. We do the same. Palette swaps via
 * `org-llm palette <name>` still propagate, because the theme JSON
 * is regenerated on every launch and opencode reflects new RGBA
 * into ctx.theme.current.
 */

// (Logo previously imported PanelBody to render the sidebar beside
// the wordmark; that path was retired in 17.1d to keep home_logo
// short so the prompt stays visible. The sidebar now renders only
// in sidebar_content, plus a one-line summary in home_bottom — both
// owned by sidebar.tsx.)

import { setPromptRef, getPromptRef, shouldShowPrompt } from "./auto-session";
import { resolveConfig, getStatus, showToast } from "./panel";
import { dispatchSysCommand } from "./sys-commands";
import { RoutePreview } from "./route-preview";

// ── Chrome bars — TNG bridge-readout look ──────────────────────────
// Real TNG pre-subspace-comms screens have varied chunky color
// blocks at top/bottom of the frame, with the emblem dominating
// the middle. These segmented bars (asymmetric widths, gaps for
// "cells" you'd see on a real LCARS panel) sit inside the chunky
// frame above and below the wordmark for that bridge-display feel.
const TOP_BAR    = "████████  ███  ██  █";
const MID_BAR_A  = "████  ";
const MID_BAR_B  = "████████  ";
const MID_BAR_C  = "███  ██";
const BOT_BAR    = "█  ████  ████████████";

// ── Contextualized prompt placeholders ─────────────────────────────
const PROMPT_PLACEHOLDERS_NORMAL = [
  "Hailing frequencies open. State your query.",
  "Have I written about… ?",
  "Did I ever capture a note on… ?",
  "What's stale in my vault right now?",
  "/insights — what org-llm noticed since last time",
  "/walk — wander my context graph",
  "/capture — save this thought",
  "Ask the vault: \"how do I feel about X\"",
  "search_notes('topic') — semantic across all notes",
  "ask_notes — RAG-grounded answer over the vault",
  "Show me recent activity (last 7 days)",
  "What did I read about <author>?",
  "/doctor — health check this workspace",
];
const PROMPT_PLACEHOLDERS_SHELL = [
  "$ org-llm <verb> — universal escape hatch",
  "$ org-llm doctor --no-diagnose",
  "$ git log --oneline -10",
  "$ rg <pattern> .",
];


/** Codepoints we know render as 2 cells in many terminals but
 * count as 1 cell in opentui's layout — using one of these as a
 * border corner glyph causes the top edge to visibly drift right
 * by one column, bumping into adjacent panels. East Asian Width
 * "Ambiguous" plus a few "Wide" symbol chars users might pick.
 * Not exhaustive — covers the geometric shapes most likely to
 * be reached for as Trek/LCARS prompt sigils. The opentui
 * library has no public width-aware setter we could delegate to.
 */
const AMBIGUOUS_WIDTH_CODEPOINTS = new Set<number>([
  0x25B6, // ▶ BLACK RIGHT-POINTING TRIANGLE
  0x25B7, // ▷ WHITE RIGHT-POINTING TRIANGLE
  0x2605, // ★ BLACK STAR
  0x2606, // ☆ WHITE STAR
  0x25C6, // ◆ BLACK DIAMOND
  0x25C7, // ◇ WHITE DIAMOND
  0x25CF, // ● BLACK CIRCLE
  0x25CB, // ○ WHITE CIRCLE
  0x2588, // █ FULL BLOCK
  0x2580, // ▀ UPPER HALF BLOCK
  0x2584, // ▄ LOWER HALF BLOCK
  0x25A0, // ■ BLACK SQUARE
  0x25A1, // □ WHITE SQUARE
  0x2660, // ♠ BLACK SPADE
  0x2663, // ♣ BLACK CLUB
  0x2665, // ♥ BLACK HEART
  0x2666, // ♦ BLACK DIAMOND
  0x2622, // ☢ RADIOACTIVE SIGN
  0x2623, // ☣ BIOHAZARD SIGN
]);

/** Build a rounded-style customBorderChars set with the top-left
 * corner replaced by the user's prompt_char. opentui's
 * `customBorderChars` lets a `<box>` override individual slots
 * while keeping the rest from its borderStyle — so we can fold
 * the Trek prompt char (❯ / › / ✦ / etc.) into the corner glyph
 * and have it render as a single-cell-wide LCARS sigil instead
 * of a separate text element with a visible gap.
 *
 * cfg.prompt_char defaults to "❯ " (with trailing space) so we
 * trim and take the first grapheme. Empty input or an Ambiguous-
 * width char → fall back to the standard rounded `╭` so the box
 * geometry stays aligned with adjacent panels. (See
 * AMBIGUOUS_WIDTH_CODEPOINTS for the list of known offenders;
 * fix the user's choice silently rather than rendering broken
 * borders that bleed into the sidebar.)
 *
 * The other 10 chars match opentui's "rounded" style (from
 * @opentui/core/lib/border BorderChars["rounded"]). We can't
 * import that constant directly without breaking the
 * @ts-nocheck/JSX-runtime setup, so the chars are inlined. They
 * match @opentui/core 0.2.0 — keep in sync if opentui ever
 * changes its rounded glyphs.
 */
function promptCharCorner(promptChar: string): {
  topLeft: string;
  topRight: string;
  bottomLeft: string;
  bottomRight: string;
  horizontal: string;
  vertical: string;
  topT: string;
  bottomT: string;
  leftT: string;
  rightT: string;
  cross: string;
} {
  const trimmed = (promptChar ?? "").trim();
  const firstGrapheme = trimmed ? Array.from(trimmed)[0] : "";
  const cp = firstGrapheme ? firstGrapheme.codePointAt(0) ?? 0 : 0;
  const isAmbiguous = AMBIGUOUS_WIDTH_CODEPOINTS.has(cp);
  const sigil = (firstGrapheme && !isAmbiguous) ? firstGrapheme : "╭";
  return {
    topLeft:     sigil,
    topRight:    "╮",
    bottomLeft:  "╰",
    bottomRight: "╯",
    horizontal:  "─",
    vertical:    "│",
    topT:        "┬",
    bottomT:     "┴",
    leftT:       "├",
    rightT:      "┤",
    cross:       "┼",
  };
}

// ── Logo composition ───────────────────────────────────────────────
//
// Smooth flowing LCARS curl. opentui's `borderStyle="rounded"` draws
// `╭ ╮ ╰ ╯ ─ │` — clean curved corners with thin sides — and we
// hug the wordmark tightly with paddingLeft/Right=1 + zero vertical
// padding, so the curve sits one cell off the block letters on
// every side. No internal stripe bars, no chunky `█` infill — just
// the wordmark, a Trek-style caps tagline, and the curve. This
// reads as a single pill panel that wraps cleanly around the logo
// the way LCARS readout panes do on a TNG bridge display.
function Logo(props: { theme: any }) {
  const t = props.theme;
  const logoColors = [
    t.primary,    // O
    t.primary,    // R
    t.accent,     // G
    t.textMuted,  // -
    t.secondary,  // L
    t.secondary,  // L
    t.warning,    // M
  ];
  return (
    <box
      border={true}
      borderStyle="rounded"
      borderColor={t.primary}
      flexDirection="column"
      alignItems="center"
      paddingLeft={1}
      paddingRight={1}
      flexShrink={0}
    >
      <ascii_font
        text="ORG-LLM"
        font="block"
        color={logoColors}
      />
      <text fg={t.textMuted}>local-first second-brain · MCP-attached</text>
    </box>
  );
}

/** Defensive wrapper for slot render functions. opencode's TUI
 * crashes the whole host if a JSX/string mismatch (or any other
 * uncaught error) reaches its renderer. We hit this when a
 * DialogPrompt description returned a raw string instead of JSX
 * — the entire TUI died with "Orphan text error" at first paint.
 *
 * `safeSlot` wraps each slot function so a crash inside our
 * rendering surfaces as a one-time toast and the slot returns
 * `null` (opencode renders nothing for that slot, which is
 * always safer than crashing the host). The user can keep
 * working; we get to ship a fix without bricking everyone.
 */
function safeSlot<F extends (...args: any[]) => any>(api: any, label: string, fn: F): F {
  let toasted = false;
  return ((...args: any[]) => {
    try {
      return fn(...args);
    } catch (e) {
      if (!toasted) {
        toasted = true;
        try {
          showToast(api, {
            variant: "error",
            title: `slot crash: ${label}`,
            message: (e as Error)?.message?.slice(0, 200) ?? "unknown error",
            duration: 12_000,
          });
        } catch {
          // last-ditch: not even toast worked. Swallow.
        }
      }
      return null;
    }
  }) as F;
}

export function registerSlots(api: any): void {
  const Prompt = api.ui.Prompt;

  api.slots.register({
    order: 1000,
    slots: {
      // home_logo — JUST the logo, nothing else.
      //
      // 17.1d landing-screen fix: an earlier iteration mounted the
      // full LCARS PanelBody beside the logo via flex-row, which
      // made the home_logo region ~50 lines tall (panel-driven).
      // opencode lays out home as a vertical column: logo / prompt /
      // bottom / footer. With home_logo absorbing all that vertical
      // space, the prompt ended up below the visible terminal area —
      // user reported "the prompt is not visible." The home-screen
      // panel-on-open requirement is now met via the home_bottom
      // slot (registered in sidebar.tsx) which renders a one-line
      // status summary BELOW the prompt without consuming the
      // prompt's vertical position. The full LCARS panel is in
      // sidebar_content (session view).
      home_logo: safeSlot(api, "home_logo",
        (ctx: any) => <Logo theme={ctx.theme.current} />),

      // home_prompt — bare <Prompt> with ref-capture + visibility
      // gate. opencode's Prompt accepts a `visible?: boolean` prop;
      // when set to false the component stays mounted (its ref
      // callback fires, our auto-submit captures it) but doesn't
      // render to screen. That's exactly what we need: during the
      // auto-session window the user sees ONLY the welcome logo —
      // no prompt flashing — but we can still programmatically
      // submit via the captured ref. Once the auto-prompt has
      // fired (or aborted), shouldShowPrompt() returns true and
      // any subsequent re-render of the slot shows the prompt.
      // (When auto_session is disabled entirely, shouldShowPrompt
      // always returns true → prompt visible normally.)
      home_prompt: safeSlot(api, "home_prompt",
        (ctx: any, data: {
          workspace_id?: string;
          ref?: (ref: unknown) => void;
        }) => {
          const t = ctx.theme.current;
          const cfg = resolveConfig(getStatus());
          const visible = shouldShowPrompt(cfg);
          // LCARS prompt outline: rounded border with prompt_char
          // FOLDED INTO the top-left corner. opentui's
          // customBorderChars lets us override individual border
          // slots — replacing `╭` with `▶` (or whatever the user
          // configures) gives us a single-cell-wide LCARS sigil
          // that sits flush with the box geometry, no gap, no
          // separate sibling element. Six iterations of trying to
          // place the char as a flex-row sibling kept hitting
          // opencode's heavy `┃` separator — this approach
          // sidesteps that entirely by living in the BORDER not
          // the input area.
          return (
            <box
              border={true}
              borderStyle="rounded"
              customBorderChars={promptCharCorner(cfg.prompt_char)}
              borderColor={t.primary}
              flexDirection="column"
            >
              <Prompt
                visible={visible}
                workspaceID={data?.workspace_id}
                ref={(r: unknown) => { setPromptRef(r); data?.ref?.(r); }}
                onSubmit={() => {
                  // Phase 18.4-iter6: parity with session_prompt's
                  // onSubmit. Without this, /sys* commands typed in
                  // the welcome prompt (or via Doom vterm injection
                  // before auto-session has navigated) get caught
                  // ONLY by the proxy's intercept_sys_commands —
                  // user sees "✓ handled locally" but the actual
                  // local action (scroll, doctor, etc.) never fires.
                  // Catching at onSubmit means dispatchSysCommand
                  // runs for EVERY submit path, regardless of which
                  // prompt slot rendered.
                  const ref = getPromptRef();
                  const text = ref?.current?.input ?? "";
                  if (dispatchSysCommand(api, text)) {
                    try { ref?.set?.({ input: "", mode: "normal", parts: [] }); } catch { /* */ }
                    return;
                  }
                  // home_prompt has no `data.on_submit` callback in
                  // the SDK type — opencode handles the submit
                  // internally. Falling through (returning) is fine.
                }}
                showPlaceholder={true}
                placeholders={{
                  normal: PROMPT_PLACEHOLDERS_NORMAL,
                  shell:  PROMPT_PLACEHOLDERS_SHELL,
                }}
                hint={<RoutePreview getRef={getPromptRef} theme={t} />}
              />
            </box>
          );
        }),

      // session_prompt — same prompt-char treatment as home_prompt
      // but for the in-session prompt input. Auto-session navigates
      // straight to session view, so without this the prompt char
      // is never visible. opencode passes session_id, visible,
      // disabled, on_submit, and ref via the slot's data arg —
      // forward all of them to the SDK <Prompt> so opencode's own
      // submit/abort plumbing keeps working.
      session_prompt: safeSlot(api, "session_prompt",
        (ctx: any, data: {
          session_id?: string;
          visible?: boolean;
          disabled?: boolean;
          on_submit?: () => void;
          ref?: (ref: unknown) => void;
        }) => {
          const t = ctx.theme.current;
          const cfg = resolveConfig(getStatus());
          // Same corner-glyph approach as home_prompt — see that
          // block for the rationale.
          return (
            <box
              border={true}
              borderStyle="rounded"
              customBorderChars={promptCharCorner(cfg.prompt_char)}
              borderColor={t.primary}
              flexDirection="column"
            >
              <Prompt
                sessionID={data?.session_id}
                visible={data?.visible}
                disabled={data?.disabled}
                ref={(r: unknown) => { setPromptRef(r); data?.ref?.(r); }}
                onSubmit={() => {
                  // PREEMPT opencode's submission for /sys*
                  // commands. If the prompt content matches one of
                  // our /sys* patterns, dispatch the action and
                  // CLEAR the prompt — never call data.on_submit,
                  // so opencode never fires the LLM round-trip.
                  // For non-/sys* messages, fall through normally.
                  // This is the most reliable way to keep typing
                  // /sysscroll-down 5 + Enter from invoking ollama
                  // (the message.updated interceptor aborts after
                  // the LLM call has already started; this prevents
                  // it from starting at all).
                  const ref = getPromptRef();
                  const text = ref?.current?.input ?? "";
                  if (dispatchSysCommand(api, text)) {
                    try { ref?.set?.({ input: "", mode: "normal", parts: [] }); } catch { /* */ }
                    return;
                  }
                  data?.on_submit?.();
                }}
                showPlaceholder={true}
                placeholders={{
                  normal: PROMPT_PLACEHOLDERS_NORMAL,
                  shell:  PROMPT_PLACEHOLDERS_SHELL,
                }}
                hint={<RoutePreview getRef={getPromptRef} theme={t} />}
              />
            </box>
          );
        }),

      // sidebar_title — per-session header.
      //
      // The visible "still broken" wrap in the user's screenshot
      // was specifically the prefix being split at the hyphen:
      // `org-` on line 1, `llm` on line 2. opencode's title cell
      // word-wrap engine treats `-` (HYPHEN-MINUS, U+002D) as a
      // valid soft-break point, so when the title overflows the
      // narrow header cell, "org-llm" is the first sacrifice.
      //
      // Fix: replace the hyphen with NON-BREAKING HYPHEN (U+2011).
      // Visually identical, but the wrap engine treats it as one
      // unbreakable unit. Plus single `<text>` element with one
      // color so no per-segment flex-wrap weirdness can fragment
      // anything — we trade per-segment coloring for reliability.
      // The whole title now renders in primary (LCARS orange)
      // which makes it pop as the org-llm session header.
      sidebar_title: safeSlot(api, "sidebar_title",
        (ctx: any, data: { title: string }) => {
          const t = ctx.theme.current;
          const title = data?.title ?? "";
          return (
            <text fg={t.primary}>{`org‑llm · ${title}`}</text>
          );
        }),
    },
  });
}
