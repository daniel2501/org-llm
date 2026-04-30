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
    // sidebar_title) in addition to the cards UI. Mock just records
    // the register() call — opencode's real runtime invokes the
    // slot functions when rendering; in tests we don't render.
    slots: { register: mock(() => () => {}) },
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

test("plugin registers slot overrides for branding (home_logo, home_prompt, sidebar_title)", async () => {
  // User reports across 2026-04-29/30: "should say org-llm not
  // opencode" + contextualize the prompt placeholder + LCARS-vary
  // the logo. Plugin registers slot overrides on every mount —
  // independent of insight cards (the branding should appear even
  // when the vault has no cards).
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);

    expect(api.slots.register).toHaveBeenCalledTimes(1);
    const arg = api.slots.register.mock.calls[0]?.[0] as
      | { order?: number; slots?: Record<string, unknown> }
      | undefined;
    expect(arg).toBeDefined();
    expect(arg?.slots).toBeDefined();
    expect(typeof arg?.slots?.home_logo).toBe("function");
    expect(typeof arg?.slots?.home_prompt).toBe("function");
    expect(typeof arg?.slots?.sidebar_title).toBe("function");
    // Slot order must beat opencode's internal-plugin defaults
    // (order: 100 per the binary bundle); otherwise our `replace`
    // doesn't actually replace.
    expect((arg?.order ?? 0)).toBeGreaterThan(100);
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
