import { test, expect, mock } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import orgLlmPlugin, { tui } from "../src/index";

// ── shape checks ─────────────────────────────────────────────────

test("named `tui` export is a function (opencode plugin contract)", () => {
  expect(typeof tui).toBe("function");
});

test("default export carries the `tui` plugin (opencode reads either)", () => {
  expect(orgLlmPlugin).toBeDefined();
  expect(typeof (orgLlmPlugin as { tui?: unknown }).tui).toBe("function");
});

// ── behavior with no insight-cards.json (plugin no-ops) ──────────

function makeTmpDir(): string {
  return mkdtempSync(join(tmpdir(), "org-llm-plugin-test-"));
}

function makeApi() {
  return {
    app: { version: "1.0.0" },
    command: { register: mock(() => () => {}), trigger: mock(() => {}), show: mock(() => {}) },
    ui: {
      toast: mock(() => {}),
      dialog: { replace: mock(() => {}), clear: mock(() => {}) },
      DialogSelect: mock(() => ({})),
      // Prompt is referenced by the home_prompt slot override; in
      // tests we just record the JSX-element-shaped marker.
      Prompt: mock((props: unknown) => ({ __mock: "Prompt", props })),
    },
    // The plugin registers branding slot overrides (home_logo,
    // home_prompt, sidebar_title) AND a Phase 17 sidebar_content
    // panel. Mock just records the register() calls — opencode's
    // real runtime invokes the slot functions when rendering; in
    // tests we don't.
    slots: { register: mock(() => () => {}) },
    // Phase 17 sidebar registers a refresh-tick disposer via
    // api.lifecycle.onDispose. The mock just stores the callback.
    lifecycle: { onDispose: mock((_fn: () => void) => () => {}) },
    state: { path: { state: "", config: "", worktree: "", directory: "" } },
  };
}

test("plugin no-ops when insight-cards.json is missing", async () => {
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    expect(api.ui.toast).not.toHaveBeenCalled();
    expect(api.command.register).not.toHaveBeenCalled();
    expect(api.ui.dialog.replace).not.toHaveBeenCalled();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin no-ops when insight-cards.json has zero cards", async () => {
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "insight-cards.json"),
      JSON.stringify({ cards: [] }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    expect(api.ui.toast).not.toHaveBeenCalled();
    expect(api.command.register).not.toHaveBeenCalled();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ── behavior with cards: toast + command + auto-open dialog ──────

test("plugin shows toast + registers /insights when cards are present", async () => {
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "insight-cards.json"),
      JSON.stringify({
        cards: [
          { kind: "stale", title: "3 stale notes", body: "look at me", suggested_question: "What's stale?" },
          { kind: "drift", title: "Tag drift", body: "x" },
        ],
      }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);

    expect(api.ui.toast).toHaveBeenCalledTimes(1);
    expect(api.command.register).toHaveBeenCalledTimes(1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin registers slot overrides for branding + Phase 17 sidebar panel", async () => {
  // User reports across 2026-04-29/30: "should say org-llm not
  // opencode" + contextualize the prompt placeholder + LCARS-vary
  // the logo + (Phase 17) live sidebar status panel with feature
  // links + sensorlog tail. Plugin registers slot overrides on
  // every mount — independent of insight cards (the branding +
  // sidebar should appear even when the vault has no cards).
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);

    // TWO registrations: the branding slots module + the sidebar
    // panel module. Both register independently so a failure in
    // one doesn't take down the other.
    expect(api.slots.register).toHaveBeenCalledTimes(2);
    type RegArg = { order?: number; slots?: Record<string, unknown> };
    const args = api.slots.register.mock.calls.map(
      (c: unknown[]) => c[0] as RegArg,
    );
    const allSlotKeys = new Set<string>();
    for (const a of args) {
      expect(a?.order ?? 0).toBeGreaterThan(100);
      for (const k of Object.keys(a?.slots ?? {})) {
        allSlotKeys.add(k);
        expect(typeof (a!.slots as Record<string, unknown>)[k])
          .toBe("function");
      }
    }
    // Branding slots
    expect(allSlotKeys.has("home_logo")).toBe(true);
    expect(allSlotKeys.has("home_prompt")).toBe(true);
    expect(allSlotKeys.has("sidebar_title")).toBe(true);
    // Phase 17 panel
    expect(allSlotKeys.has("sidebar_content")).toBe(true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin AUTO-OPENS the dialog on mount (cards appear on open)", async () => {
  // The user's request: insight cards must appear on open without the
  // user having to type /insights. The plugin schedules the dialog
  // open via setTimeout(..., 0) so opencode's UI mounts first; this
  // test waits past the timer and asserts dialog.replace was called.
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "insight-cards.json"),
      JSON.stringify({
        cards: [
          { kind: "stale", title: "3 stale notes", body: "look at me" },
        ],
      }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    // Yield the event loop so the setTimeout(..., 0) fires.
    await new Promise((r) => setTimeout(r, 10));

    expect(api.ui.dialog.replace).toHaveBeenCalledTimes(1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
