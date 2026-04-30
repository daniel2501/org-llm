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
 *
 * Slots overridden (per opencode's TuiHostSlotMap in
 * @opencode-ai/plugin/dist/tui.d.ts):
 *   • home_logo    — the big logo on the welcome screen
 *   • home_prompt  — the "Ask anything..." input box (custom placeholders)
 *   • sidebar_title — the sidebar header per session
 *
 * Implementation notes:
 *   • opencode's plugin host expects:
 *       { order, slots: { <slotName>: (ctx, data) => JSX.Element } }
 *   • JSX intrinsic elements come from @opentui/solid (box, text,
 *     ascii_font, span). tsconfig sets jsxImportSource accordingly.
 *   • Use <ascii_font> for the wordmark so opentui handles letter
 *     alignment — the prior raw-ASCII embedding had inconsistent
 *     leading whitespace and rendered misaligned in the live TUI.
 */

import type { TuiPluginApi } from "@opencode-ai/plugin/tui";

// ── LCARS palette anchors ───────────────────────────────────────────
// We name the three primary LCARS roles by the theme's role colors so
// the slot rendering tracks `org-llm palette <name>` swaps without
// hard-coded hex. opencode's theme system resolves "primary" /
// "secondary" / "accent" against the active theme (org-llm-lcars or
// whatever the user set).
const LCARS_PRIMARY = "primary"   as const; // lcars1 — orange
const LCARS_SECONDARY = "secondary" as const; // lcars2 — purple
const LCARS_ACCENT = "accent"    as const; // lcars3 — blue
const LCARS_WARN = "warning"   as const; // gold-ish
const LCARS_INFO = "info"      as const;

// Per-letter color cycling for the wordmark — opentui's ASCIIFont
// accepts `color: ColorInput[]` and rotates the array across letters,
// so this gives ORG·-·LLM a varied LCARS palette instead of one flat
// fill. Roles match: ORG in primary/accent, dash in muted, LLM in
// secondary/warn for the iconic LCARS multi-tone block.
const LOGO_COLORS = [
  LCARS_PRIMARY,    // O
  LCARS_PRIMARY,    // R
  LCARS_ACCENT,     // G
  "textMuted",      // -
  LCARS_SECONDARY,  // L
  LCARS_SECONDARY,  // L
  LCARS_WARN,       // M
];

// ── Chrome bars — varied widths in classic LCARS layout ─────────────
// Real LCARS interfaces stack chunky horizontal bars of different
// lengths and colors. We emulate that with three lines of varied
// segments — top wide, middle short-and-thick, bottom asymmetric.
const TOP_BAR    = "▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇  ▇▇▇▇▇▇▇▇▇  ▇▇▇▇  ▇▇";
const MID_BAR_A  = "▇▇▇▇▇▇▇▇  ";
const MID_BAR_B  = "▇▇▇▇▇▇▇▇▇▇▇▇▇▇  ";
const MID_BAR_C  = "▇▇▇  ▇▇";
const BOT_BAR    = "▇▇  ▇▇▇▇  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇";

// ── Contextualized prompt placeholders ─────────────────────────────
// opencode's default placeholder rotation is generic ("Fix broken
// tests" / "Ask anything…"). These are tailored to the org-llm
// vocabulary so the user is reminded what THIS workspace is for the
// moment they look at the input box. Mix of:
//   • verb hints that map to MCP tools (search_notes, ask_notes…)
//   • slash-command nudges (/insights, /walk)
//   • themed (LCARS / Trek) phrasings to match the CLI persona
// Kept under 60 chars each so they don't truncate on narrow terminals.
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

export function registerSlots(api: TuiPluginApi): void {
  // Capture the Prompt component for use in JSX. Solid's JSX expects
  // capitalized component identifiers; aliasing through a local const
  // is the canonical pattern.
  const Prompt = api.ui.Prompt;

  api.slots.register({
    // opencode's internal slot plugins register at order: 100 (per
    // the binary's compiled bundle, e.g. internal:home-footer +
    // internal:sidebar-content). We need to BEAT them for `replace`
    // mode, so register higher. 1000 leaves room for user plugins
    // that want to layer above us.
    order: 1000,
    slots: {
      // home_logo — varied LCARS chrome + multi-color ascii_font
      // wordmark. Layout (each row is its own colored segment so the
      // composition reads as proper LCARS rather than monochrome
      // text):
      //
      //   ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇  ▇▇▇▇▇▇▇▇▇  ▇▇▇▇  ▇▇    ← top bar (orange)
      //          O  R  G  -  L  L  M                ← ascii_font, per-letter colors
      //   ▇▇▇▇▇▇▇▇        ▇▇▇▇▇▇▇▇▇▇▇▇▇▇        ▇▇▇  ▇▇   ← mid bars (purple/blue/gold)
      //   ▇▇  ▇▇▇▇  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇   ← bottom bar (orange)
      //   local-first second-brain · MCP-attached    ← muted tagline
      home_logo: () => (
        <box flexDirection="column" alignItems="center" paddingTop={1}>
          <text fg={LCARS_PRIMARY}>{TOP_BAR}</text>
          <ascii_font
            text="ORG-LLM"
            font="block"
            color={LOGO_COLORS}
          />
          <box flexDirection="row">
            <text fg={LCARS_SECONDARY}>{MID_BAR_A}</text>
            <text fg={LCARS_ACCENT}>{MID_BAR_B}</text>
            <text fg={LCARS_WARN}>{MID_BAR_C}</text>
          </box>
          <text fg={LCARS_PRIMARY}>{BOT_BAR}</text>
          <text fg="textMuted">local-first second-brain · MCP-attached</text>
        </box>
      ),

      // home_prompt — full prompt replacement so we can swap in
      // org-llm-flavored placeholder strings. opencode's default
      // placeholders ("Fix broken tests" etc.) are coding-agent-y;
      // ours match the vault verb vocabulary and slash commands.
      // workspaceID + ref are what opencode passes us; both are
      // forwarded to the Prompt so opencode's session wiring stays
      // intact.
      home_prompt: (_ctx: unknown, data: {
        workspace_id?: string;
        ref?: (ref: unknown) => void;
      }) => (
        <Prompt
          workspaceID={data?.workspace_id}
          ref={data?.ref}
          showPlaceholder={true}
          placeholders={{
            normal: PROMPT_PLACEHOLDERS_NORMAL,
            shell:  PROMPT_PLACEHOLDERS_SHELL,
          }}
        />
      ),

      // sidebar_title — per-session header. Prepends "org-llm •" so
      // every session is visibly an org-llm session.
      sidebar_title: (_ctx: unknown, data: { title: string }) => (
        <box flexDirection="row">
          <text fg={LCARS_PRIMARY}>org-llm</text>
          <text fg="textMuted"> • </text>
          <text>{data?.title ?? ""}</text>
        </box>
      ),
    },
  } as unknown as Parameters<typeof api.slots.register>[0]);
}
