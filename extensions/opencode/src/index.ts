/**
 * @org-llm/opencode-plugin — entry point.
 *
 * Phase 16 scaffolding. Real plugin implementations land in subsequent
 * commits. The shape:
 *
 *   - registerHooks(opencodeAPI)     — hook into session-start, tool-call,
 *                                       theme-change events
 *   - lifeSupportPanel(probes)       — render life-support inline (Phase 10
 *                                       integration so vitals show in the
 *                                       TUI without leaving opencode)
 *   - insightCardsFirstMessage()     — Phase 12.4 path; currently we inject
 *                                       via the system prompt, but a real
 *                                       plugin can render cards as a UI
 *                                       widget instead
 *   - modelSelectionGuard(modelID)   — refuse opencode's bundled big-pickle
 *                                       fallback when an Ollama provider is
 *                                       configured (the bug we hit in 2026-04-29)
 *
 * Entry point is the default export. opencode's plugin loader expects a
 * function that takes the runtime context and returns either void or a
 * disposer function.
 */

export interface OrgLlmPluginContext {
  // Filled in once we read packages/opencode/src/plugin/index.ts to learn
  // the actual context shape opencode passes to plugins.
  readonly opencodeVersion?: string;
}

export default function orgLlmPlugin(_ctx: OrgLlmPluginContext): void {
  // Phase 16.1 lands the first real hook here.
  //
  // For now this is a no-op placeholder so the build/test/typecheck
  // pipeline produces a working artifact end-to-end before any
  // behavior code lands.
  return;
}
