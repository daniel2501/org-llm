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
    // home_prompt, sidebar_title) AND the Phase 17.1 sidebar panel
    // (sidebar_content + home_bottom). Mock just records the
    // register() calls — opencode's real runtime invokes the slot
    // functions when rendering; in tests we don't.
    slots: { register: mock(() => () => {}) },
    // Phase 17.1 deactivates the four internal sidebar plugins it
    // replaces (sidebar-context stays per user feedback). Mock
    // returns a resolved Promise<true> so the fire-and-forget call
    // doesn't reject in tests.
    plugins: { deactivate: mock(async (_id: string) => true) },
    // Phase 17 sidebar registers a refresh-tick disposer via
    // api.lifecycle.onDispose. The mock just stores the callback.
    lifecycle: { onDispose: mock((_fn: () => void) => () => {}) },
    state: { path: { state: "", config: "", worktree: "", directory: "" } },
  };
}

test("plugin no-ops insight-cards toast + dialog when insight-cards.json is missing", async () => {
  // Insight-cards-specific behaviors (toast + dialog + /insights
  // command) shouldn't fire when no cards file exists. The
  // sys-commands registration (Phase 17.1k) is independent — it
  // ALWAYS registers /sys, /sysdoctor, etc. since those run CLI
  // subcommands directly, not LLM-driven flows. So
  // command.register fires once for sys-commands but should NOT
  // fire for /insights. Track which commands got registered.
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    expect(api.ui.toast).not.toHaveBeenCalled();
    expect(api.ui.dialog.replace).not.toHaveBeenCalled();
    // Sys-commands registers exactly one command-list with all
    // /sys* slash commands. /insights would be a SECOND register
    // call when cards are present.
    const cmdNames = api.command.register.mock.calls.flatMap(
      (c: unknown[]) => {
        const cb = c[0] as () => Array<{ slash?: { name: string } }>;
        return cb().map((cmd) => cmd.slash?.name).filter(Boolean);
      },
    );
    expect(cmdNames).not.toContain("insights");
    expect(cmdNames).toContain("sysrun");
    expect(cmdNames).toContain("sysdoctor");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin no-ops insight-cards behaviors when zero cards (sys-commands still register)", async () => {
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
    const cmdNames = api.command.register.mock.calls.flatMap(
      (c: unknown[]) => {
        const cb = c[0] as () => Array<{ slash?: { name: string } }>;
        return cb().map((cmd) => cmd.slash?.name).filter(Boolean);
      },
    );
    expect(cmdNames).not.toContain("insights");
    expect(cmdNames).toContain("sysrun");
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
    // Two registrations now: one for /insights (cards present)
    // and one for /sys* sys-commands (always).
    expect(api.command.register).toHaveBeenCalledTimes(2);
    const cmdNames = api.command.register.mock.calls.flatMap(
      (c: unknown[]) => {
        const cb = c[0] as () => Array<{ slash?: { name: string } }>;
        return cb().map((cmd) => cmd.slash?.name).filter(Boolean);
      },
    );
    expect(cmdNames).toContain("insights");
    expect(cmdNames).toContain("sysrun");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin registers branding slots + Phase 17.1 sidebar_content panel", async () => {
  // Phase 17.1 architecture:
  //   • slots.tsx owns home_logo / home_prompt / sidebar_title.
  //     The home-screen panel is rendered INSIDE home_logo (beside
  //     the logo via horizontal flex) — opencode 1.14.31's home
  //     view has no native sidebar slot, but home_logo is in
  //     replace mode and owns the region above the prompt.
  //   • sidebar.tsx owns sidebar_content (the in-session sidebar).
  // Both share PanelBody from panel.tsx.
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);

    // TWO registrations: branding slots + sidebar_content. Both
    // register independently so a failure in one doesn't take down
    // the other.
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
    // Branding slots — always registered.
    expect(allSlotKeys.has("home_logo")).toBe(true);
    expect(allSlotKeys.has("home_prompt")).toBe(true);
    expect(allSlotKeys.has("session_prompt")).toBe(true);
    expect(allSlotKeys.has("sidebar_title")).toBe(true);
    // Phase 17.1 in-session sidebar.
    expect(allSlotKeys.has("sidebar_content")).toBe(true);
    // 17.1d: home_bottom carries a single-line status banner
    // (stardate + indexed % + model). Earlier iterations mounted
    // the full panel beside the logo via home_logo, but that
    // pushed the prompt off-screen. The banner sits below the
    // prompt — visible on open without consuming the prompt's
    // vertical position.
    expect(allSlotKeys.has("home_bottom")).toBe(true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin deactivates the 4 internal sidebar widgets it replaces (KEEPS context)", async () => {
  // Phase 17.1: opencode 1.14.31's sidebar_content slot accepts
  // multiple appended widgets. Default replace list is mcp/lsp/todo/files;
  // SUBSYSTEMS / ARCHIVE / ENGAGE cover that ground in our LCARS
  // panel. sidebar-context is preserved per user feedback
  // 2026-05-01 (it carries token usage + session cost we don't
  // surface elsewhere).
  const dir = makeTmpDir();
  try {
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    // Yield once so the fire-and-forget deactivate() promises run.
    await new Promise((r) => setTimeout(r, 0));

    const ids = api.plugins.deactivate.mock.calls.map(
      (c: unknown[]) => c[0],
    );
    expect(ids).toContain("internal:sidebar-mcp");
    expect(ids).toContain("internal:sidebar-lsp");
    expect(ids).toContain("internal:sidebar-todo");
    expect(ids).toContain("internal:sidebar-files");
    // Critical: KEEP context — token usage + cost panel must remain.
    expect(ids).not.toContain("internal:sidebar-context");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("config.panel_enabled=false skips sidebar_content + skips deactivation", async () => {
  // The master kill switch. When the literate config sets
  // sidebar_panel_enabled=false, registerSidebar must NOT register
  // sidebar_content and must NOT deactivate any internal plugins.
  // (Branding slots in slots.tsx still register — they're
  // independent, and home_logo is always registered as the logo
  // override regardless of panel_enabled. The home-screen panel
  // *render* inside home_logo is gated at render time by the same
  // flag; that's a runtime concern not visible to this test.)
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "sidebar-status.json"),
      JSON.stringify({ config: { panel_enabled: false } }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    await new Promise((r) => setTimeout(r, 0));

    // Only the slots.tsx branding registration runs; sidebar.tsx
    // short-circuits before slots.register.
    expect(api.slots.register).toHaveBeenCalledTimes(1);
    type RegArg = { slots?: Record<string, unknown> };
    const args = api.slots.register.mock.calls.map(
      (c: unknown[]) => c[0] as RegArg,
    );
    const allSlotKeys = new Set<string>();
    for (const a of args) {
      for (const k of Object.keys(a?.slots ?? {})) allSlotKeys.add(k);
    }
    expect(allSlotKeys.has("sidebar_content")).toBe(false);
    // home_logo is always registered (branding); ensure that
    // didn't accidentally get gated off.
    expect(allSlotKeys.has("home_logo")).toBe(true);
    expect(api.plugins.deactivate).not.toHaveBeenCalled();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("config.panel_on_session=false skips sidebar_content registration", async () => {
  // panel_on_session is the registration-time gate for the
  // in-session sidebar slot. panel_on_home is a render-time gate
  // inside home_logo (always registered as the logo override) and
  // can't be checked at registration time — the slot function
  // makes the runtime decision based on the cached status.
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "sidebar-status.json"),
      JSON.stringify({
        config: {
          panel_enabled: true,
          panel_on_home: true,
          panel_on_session: false,
          replace_internal: [],
        },
      }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);

    type RegArg = { slots?: Record<string, unknown> };
    const args = api.slots.register.mock.calls.map(
      (c: unknown[]) => c[0] as RegArg,
    );
    const allSlotKeys = new Set<string>();
    for (const a of args) {
      for (const k of Object.keys(a?.slots ?? {})) allSlotKeys.add(k);
    }
    // sidebar_content must NOT register when panel_on_session=false.
    expect(allSlotKeys.has("sidebar_content")).toBe(false);
    // Branding slots still register.
    expect(allSlotKeys.has("home_logo")).toBe(true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("config.replace_internal flows through to plugins.deactivate (with internal: prefix)", async () => {
  // User config sets a non-default replace_internal list — only the
  // listed IDs should be deactivated, with the "internal:" prefix
  // applied by the TS side (the csv stores the bare names).
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    writeFileSync(
      join(dir, ".opencode", "sidebar-status.json"),
      JSON.stringify({
        config: {
          panel_enabled: true,
          replace_internal: ["sidebar-files", "sidebar-todo"],
        },
      }),
    );
    const api = makeApi();
    api.state.path.directory = dir;
    await tui(api as unknown as Parameters<typeof tui>[0]);
    await new Promise((r) => setTimeout(r, 0));

    const ids = api.plugins.deactivate.mock.calls.map(
      (c: unknown[]) => c[0],
    );
    expect(ids).toContain("internal:sidebar-files");
    expect(ids).toContain("internal:sidebar-todo");
    expect(ids).not.toContain("internal:sidebar-mcp");
    expect(ids).not.toContain("internal:sidebar-lsp");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("plugin auto-opens the dialog on mount only when auto_open=true", async () => {
  // The user's request: insight cards must appear on open without the
  // user having to type /insights. The plugin schedules the dialog
  // open via setTimeout(..., 0) so opencode's UI mounts first; this
  // test waits past the timer and asserts dialog.replace was called.
  const dir = makeTmpDir();
  try {
    mkdirSync(join(dir, ".opencode"));
    // Phase 18.5: auto-open is opt-in. With auto_open=true the
    // plugin pops the modal; default (false / absent) → no modal,
    // toast-only.
    writeFileSync(
      join(dir, ".opencode", "insight-cards.json"),
      JSON.stringify({
        auto_open: true,
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

    // Now with auto_open absent (= default false) — no modal.
    writeFileSync(
      join(dir, ".opencode", "insight-cards.json"),
      JSON.stringify({
        cards: [
          { kind: "stale", title: "3 stale notes", body: "look at me" },
        ],
      }),
    );
    const api2 = makeApi();
    api2.state.path.directory = dir;
    await tui(api2 as unknown as Parameters<typeof tui>[0]);
    await new Promise((r) => setTimeout(r, 10));
    expect(api2.ui.dialog.replace).not.toHaveBeenCalled();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
