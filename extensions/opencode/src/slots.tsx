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
// at TUI launch, so `<box>` and `<text fg="primary">` ARE interpreted
// as opentui Renderables when opencode invokes our slot functions
// (verified via `bun -e "import(...).then(m => m.registerSlots(...))"`).
// Only the TS checker is confused. `@ts-nocheck` is the smallest
// blast-radius fix until @opentui/solid ships a real jsx-runtime.js.
//
/**
 * @org-llm/opencode-plugin — slot overrides.
 *
 * Replaces opencode's built-in TUI surfaces with org-llm branding.
 * The user reported on 2026-04-29 that opencode's home screen + sidebar
 * still said "opencode" even though the chat-side identity prompt
 * already overrode the model's self-introduction.
 *
 * Slots overridden (per opencode's TuiHostSlotMap in
 * @opencode-ai/plugin/dist/tui.d.ts):
 *   • home_logo    — the big logo on the welcome screen
 *   • sidebar_title — the sidebar header per session
 *
 * Implementation notes:
 *   • opencode's plugin host expects a SolidPlugin shape:
 *       { order, slots: { <slotName>: (ctx, data) => JSX.Element } }
 *     The id is forbidden ({ id?: never }) — opencode generates it.
 *   • JSX intrinsic elements come from @opentui/solid (box, text,
 *     ascii_font, span). tsconfig sets jsxImportSource accordingly.
 *   • Solid evaluates expressions reactively — keep slot bodies pure
 *     (no side effects, no api calls).
 */

import type { TuiPluginApi } from "@opencode-ai/plugin/tui";

// ── ASCII brand block — kept terse so it doesn't dominate the
// welcome screen on smaller terminals. The LCARS chunk-bar sits
// underneath as a chrome accent matching the CLI splash.
const LOGO_ASCII = `
   ██████╗ ██████╗  ██████╗       ██╗     ██╗     ███╗   ███╗
  ██╔═══██╗██╔══██╗██╔════╝       ██║     ██║     ████╗ ████║
  ██║   ██║██████╔╝██║  ███╗█████╗██║     ██║     ██╔████╔██║
  ██║   ██║██╔══██╗██║   ██║╚════╝██║     ██║     ██║╚██╔╝██║
  ╚██████╔╝██║  ██║╚██████╔╝      ███████╗███████╗██║ ╚═╝ ██║
   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝       ╚══════╝╚══════╝╚═╝     ╚═╝
`.trim();

const LCARS_BAR = "▆▆▆▆▆▆▆▆▆▆ ▆▆▆▆▆▆▆ ▆▆▆▆ ▆▆▆ ▆▆";

export function registerSlots(api: TuiPluginApi): void {
  // The slot registry is keyed off opencode's TuiHostSlotMap. We only
  // override the surfaces the user flagged; everything else falls
  // through to opencode's defaults.
  api.slots.register({
    // opencode's internal slot plugins register at order: 100 (per
    // the binary's compiled bundle, e.g. internal:home-footer +
    // internal:sidebar-content). We need to BEAT them for `replace`
    // mode, so register higher. 1000 leaves room for user plugins
    // that want to layer above us.
    order: 1000,
    slots: {
      // home_logo — the prominent landing-page logo. opencode's
      // default reads "opencode"; replace with org-llm wordmark plus
      // an LCARS-themed accent bar that matches the CLI splash.
      home_logo: () => (
        <box flexDirection="column" alignItems="center" paddingTop={1}>
          <text fg="primary">{LOGO_ASCII}</text>
          <text fg="secondary">{LCARS_BAR}</text>
          <text fg="textMuted">local-first second-brain · MCP-attached</text>
        </box>
      ),

      // sidebar_title — per-session header. opencode's default shows
      // the session title with no app branding. Prepend "org-llm •"
      // so the user always sees they're in the org-llm session, not
      // a generic opencode session.
      sidebar_title: (_ctx: unknown, data: { title: string }) => (
        <box flexDirection="row">
          <text fg="primary">org-llm</text>
          <text fg="textMuted"> • </text>
          <text>{data?.title ?? ""}</text>
        </box>
      ),
    },
  } as unknown as Parameters<typeof api.slots.register>[0]);
  // The `as unknown as` cast bridges TuiHostSlotMap's structural
  // type to the registry's stricter generic. opencode's runtime is
  // permissive — it just iterates over the slots dict.
}
