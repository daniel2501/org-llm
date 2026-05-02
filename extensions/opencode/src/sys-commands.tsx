// @ts-nocheck
//
// Same JSX-runtime / @ts-nocheck rationale as the rest of the
// plugin — see slots.tsx for the full explanation.
//
/**
 * @org-llm/opencode-plugin — sys-commands (Phase 17.1k).
 *
 * Helpers for running org-llm CLI subcommands as side-effect
 * actions and injecting their output into the chat as zero-cost
 * (noReply) user messages. Used by:
 *
 *   • slow-llm-watch.tsx — auto-runs `doctor --power-boost` when
 *     the LLM hangs past the configured threshold, then injects
 *     the diagnosis into chat AND auto-applies the cloud-routing
 *     fix.
 *
 *   • /sysdoctor, /sysstats, etc. — slash commands the user can
 *     invoke manually to see CLI subcommand output in chat
 *     without round-tripping through the LLM.
 *
 * The point: opencode's chat surface is the user's primary
 * interface — diagnostic output, recent activity, model lists, all
 * of these belong in chat alongside the conversation, not in
 * transient toasts/dialogs that vanish on Esc.
 */

import { readdirSync, readFileSync } from "node:fs";
import { getPromptRef } from "./auto-session";
import { applyCloudFix, getCachedProposals, applyProposal } from "./slow-llm-watch";
import { resolveConfig, getStatus, scrollSidebar, showToast } from "./panel";

/** Strip ANSI color codes from text. CLI subcommands print
 * themed output; in chat we want plain text. */
export function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

/** Frame color slots — kept as a typed enum so callers can pick a
 * semantic color (PRIMARY for normal, WARNING for failures, etc.)
 * without knowing the rendering details.
 *
 * 17.1r-iter4: ANSI 24-bit color codes were tried here for
 * coloured frames in chat. Result: opencode's chat surface
 * strips the ESC byte (\x1b) but LEAVES the parameter chars
 * (`[38;2;255;204;153m`) visible in the rendered output —
 * literal escape-sequence garbage in the user's chat. Disabled.
 * Codes are now empty strings; frames render in plain monochrome
 * box-drawing characters (which renders cleanly).
 *
 * If opencode ever exposes a real way to colour text in user
 * messages (markdown extensions, JSON-encoded color metadata,
 * etc.), repopulate these and the frames pick it up immediately. */
const ANSI = {
  PRIMARY:   "",
  ACCENT:    "",
  WARNING:   "",
  SECONDARY: "",
  RESET:     "",
} as const;

/** Read the current chat-styling config (emojis on/off, frames
 * on/off). Cached read of the sidebar-status JSON via panel.tsx.
 * Helper so each chat-injection site doesn't re-import the cfg
 * machinery. */
function chatStyle(): { emojis: boolean; frames: boolean } {
  const cfg = resolveConfig(getStatus());
  return { emojis: cfg.chat_emojis, frames: cfg.chat_frames };
}

/** Strip emoji from a title when emojis are disabled. Drops the
 * leading emoji + space; leaves alphabetic characters intact.
 * Used so titles like "🩺 AUTO-DOCTOR" cleanly degrade to
 * "AUTO-DOCTOR" when chat_emojis=false. */
function maybeStripEmoji(title: string, emojisOn: boolean): string {
  if (emojisOn) return title;
  // Strip an initial emoji-presentation char or a sequence of
  // them (followed by a space) from the title. Conservative
  // pattern — covers the chars we use without being aggressive.
  return title.replace(/^[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2300}-\u{23FF}]+\s*/u, "");
}

/** Wrap a body of text lines in a rounded LCARS-style frame with
 * a coloured title corner — OR a flat indented body when
 * cfg.chat_frames=false. Emoji titles drop their prefix when
 * cfg.chat_emojis=false. Both knobs are read from getStatus() at
 * render time so the user can flip them mid-session via
 * `org-llm config sidebar_chat_*` without relaunching.
 *
 * Width auto-fits to the longest body line plus padding (min 56
 * cols so titles don't look stranded). Output is a list of
 * lines suitable for `lines.join("\n")` injection.
 *
 * Color is best-effort — if opencode strips ANSI, the frame
 * still renders correctly in plain monochrome. The structure
 * (╭ ─ ╮ │ ╰ ─ ╯) is what carries the visual separation. */
export function frame(
  title: string,
  body: string[],
  color: keyof typeof ANSI = "PRIMARY",
): string[] {
  const { emojis, frames } = chatStyle();
  const t = maybeStripEmoji(title, emojis);

  if (!frames) {
    // Flat layout — header line + horizontal rule + indented body.
    // Same structure as a frame, just without the box.
    const rule = "─".repeat(56);
    return [t, rule, "", ...body.map((l) => `  ${l}`), ""];
  }

  const c = ANSI[color];
  const r = ANSI.RESET;
  const longestBody = body.reduce((m, l) => Math.max(m, l.length), 0);
  const width = Math.max(56, longestBody + 4, t.length + 6);
  const titleSep = "─".repeat(Math.max(1, width - t.length - 4));
  const top = `${c}╭─ ${t} ${titleSep}╮${r}`;
  const mid = body.map((l) =>
    `${c}│${r} ${l.padEnd(width - 2)} ${c}│${r}`,
  );
  const bot = `${c}╰${"─".repeat(width)}╯${r}`;
  return [top, ...mid, bot];
}

/** Parse the argument string of `/sysapply <args>` into a list of
 * proposal IDs. Accepts comma- or space-separated numbers plus
 * `<lo>-<hi>` ranges. Examples:
 *   "1"        → [1]
 *   "1,3"      → [1,3]
 *   "1 3"      → [1,3]
 *   "1-3"      → [1,2,3]
 *   "1,3-5"    → [1,3,4,5]
 *   "all"      → []  (caller treats empty as "no specific selection")
 *
 * Out-of-range or non-numeric tokens silently drop. The caller
 * checks each ID against the cached proposal list and surfaces a
 * "no such proposal" message for misses, so loose parsing here
 * is fine. */
function parseProposalNumbers(raw: string): number[] {
  const out: number[] = [];
  for (const token of raw.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean)) {
    const range = token.match(/^(\d+)-(\d+)$/);
    if (range) {
      const lo = parseInt(range[1], 10);
      const hi = parseInt(range[2], 10);
      if (Number.isFinite(lo) && Number.isFinite(hi) && lo <= hi) {
        for (let i = lo; i <= hi; i++) out.push(i);
      }
      continue;
    }
    const n = parseInt(token, 10);
    if (Number.isFinite(n)) out.push(n);
  }
  return out;
}

/** Set the prompt's textbox content from a slash-command onSelect
 * handler. Used for arg-bearing slashes (/sysmodel, /sysapply)
 * where we want autocomplete to leave the prompt populated so
 * the user can keep typing arguments. The SDK's TuiPromptRef.set()
 * takes a TuiPromptInfo with `input`, `mode`, `parts`.
 *
 * NOTE: deliberately does NOT submit. Earlier iteration had a
 * `submit` flag that called ref.submit() — that piped the slash
 * through opencode's normal submission pipeline, which triggers
 * the LLM call regardless of our message.updated interceptor.
 * On slow local Ollama that's a multi-second hang for what
 * should be sub-millisecond local actions. Use clearPromptAnd()
 * + direct action helpers for zero-arg slashes. */
export function setPromptInput(text: string): void {
  const ref = getPromptRef();
  if (!ref?.set) return;
  try {
    ref.set({ input: text, mode: "normal", parts: [] });
    ref.focus?.();
  } catch {
    // ref method threw — silently no-op. The user can still type
    // the literal text.
  }
}

/** Read the current prompt text via the captured ref. Used by
 * arg-bearing onSelect handlers to extract args the user typed
 * before TAB-selecting the slash. Returns "" if ref isn't
 * captured yet (panel not mounted). */
function readPromptInput(): string {
  try {
    const ref = getPromptRef();
    return ref?.current?.input ?? "";
  } catch {
    return "";
  }
}

/** Clear the prompt textbox via the captured ref. Called by
 * onSelect handlers AFTER running their action — leaves the
 * input empty so opencode doesn't submit anything to the LLM.
 * The user sees a clean prompt, which is the visual signal
 * "your slash command was handled." */
function clearPrompt(): void {
  try {
    const ref = getPromptRef();
    ref?.set?.({ input: "", mode: "normal", parts: [] });
  } catch {
    // best-effort
  }
}

/** Run an org-llm CLI subcommand and capture its stdout.
 * 5s timeout — these commands should be sub-second; anything
 * slower is a hung child we don't wait on. Errors return a
 * placeholder message so callers can always proceed. */
export async function runOrgLlm(args: string[]): Promise<string> {
  try {
    const proc = Bun.spawn(["org-llm", ...args], {
      stdout: "pipe",
      stderr: "pipe",
    });
    const timeout = new Promise<string>((resolve) =>
      setTimeout(() => resolve("(subcommand timed out after 5s)"), 5_000),
    );
    const text = (async () => {
      const out = await new Response(proc.stdout).text();
      return stripAnsi(out).trim();
    })();
    return await Promise.race([text, timeout]);
  } catch (err) {
    return `(could not run org-llm ${args.join(" ")}: ${(err as Error)?.message ?? "unknown"})`;
  }
}

/** Inject a text message into the active chat session via the
 * SDK with `noReply: true` — opencode stores the message in chat
 * history but does NOT trigger an AI response. Sub-millisecond,
 * survives the chat history (unlike toasts/dialogs). */
export async function injectChatMessage(
  api: any,
  sessionID: string,
  text: string,
): Promise<boolean> {
  try {
    await api.client?.session?.prompt?.({
      sessionID,
      noReply: true,
      parts: [{ type: "text", text }],
    });
    return true;
  } catch {
    return false;
  }
}

/** Get the active session ID from the route, or null if user is
 * on the home view (no session yet). */
export function activeSessionID(api: any): string | null {
  const r = api.route?.current;
  if (r?.name === "session" && typeof r?.params?.sessionID === "string") {
    return r.params.sessionID;
  }
  return null;
}

/** Run a sub-command and inject its output as a chat message.
 * Falls back to a toast if no active session. Returns true if
 * the chat injection succeeded.
 */
export async function runAndInject(
  api: any,
  args: string[],
  heading: string,
): Promise<boolean> {
  const sessionID = activeSessionID(api);
  if (!sessionID) {
    showToast(api, {
      variant: "warning",
      title: "No active session",
      message: `Open a chat session first to run \`${heading}\` — output will inject into chat.`,
      duration: 4_000,
    });
    return false;
  }
  // Quick "running" toast so the user sees something is happening.
  showToast(api, {
    variant: "info",
    title: heading,
    message: `Running 'org-llm ${args.join(" ")}'…`,
    duration: 3_000,
  });
  const output = await runOrgLlm(args);
  const bodyLines = output.slice(0, 4_000).split("\n");
  bodyLines.push("");
  bodyLines.push(`(zero-cost: ran 'org-llm ${args.join(" ")}', no LLM call)`);
  const framed = frame(`⚙ ${heading}`, bodyLines, "PRIMARY");
  return await injectChatMessage(api, sessionID, framed.join("\n"));
}

/** Switch chat_model to a user-specified local model + relaunch
 * opencode. Mirrors applyCloudFix's structure: persist config,
 * inject confirmation, spawn detached relaunch, exit current
 * process. Used by /sysmodel <name> (manual swap) and the
 * auto-doctor (when doctor proposes a downsize/upsize). */
export async function runSwitchLocalModel(
  api: any,
  sessionID: string,
  modelName: string,
): Promise<void> {
  if (!modelName) {
    await injectChatMessage(api, sessionID,
      "  [!] Usage:  /sysmodel <model-name>   e.g. /sysmodel llama3.2:1b",
    );
    return;
  }

  showToast(api, {
    variant: "info",
    title: "Local model swap",
    message: `Switching to ${modelName}, relaunching opencode in 3s.`,
    duration: 5_000,
  });

  // Persist chat_model. Unlike the cloud knob (which we treat as
  // one-shot), a model swap IS what the user wants persisted —
  // they explicitly typed the model name.
  let configOk = false;
  try {
    const proc = Bun.spawn(
      ["org-llm", "config", "chat_model", modelName],
      { stdout: "pipe", stderr: "pipe" },
    );
    await proc.exited;
    configOk = proc.exitCode === 0;
  } catch {
    configOk = false;
  }

  const body: string[] = [];
  if (configOk) {
    body.push(`✓ Persisted  chat_model = ${modelName}`);
  } else {
    body.push("✗ Failed to persist config. Run manually:");
    body.push(`    org-llm config chat_model ${modelName}`);
  }
  body.push("→ Relaunching 'org-llm launch' in 3s…");
  body.push("");
  body.push("Cloud routing unchanged.");
  const framed = frame("🔄 LOCAL MODEL SWAP", body, configOk ? "PRIMARY" : "WARNING");
  await injectChatMessage(api, sessionID, framed.join("\n"));

  if (!configOk) return;
  // Drop relaunch marker — cli.py's launch() loop sees it after
  // opencode returns and re-execs itself. Same mechanism as
  // applyCloudFix; ensures TTY transfers cleanly. No `cloud`
  // flag in the marker → cli.py relaunches in default mode,
  // which picks up the new chat_model we just persisted.
  try {
    const home = process.env.HOME ?? "";
    const marker = `${home}/.local/share/org-llm/relaunch-marker.json`;
    await Bun.write(marker, JSON.stringify({}));
  } catch {
    // best-effort; fall through to exit
  }
  setTimeout(() => {
    try { process.exit(0); } catch {
      try { process.kill(process.ppid ?? 0, "SIGTERM"); } catch { /* */ }
    }
  }, 1500);
}

/** Parse a prompt-text string and dispatch the matching /sys*
 * action. Returns true if dispatched (caller should NOT fall
 * through to opencode's normal submission pipeline — the action
 * handles its own chat injection / relaunch / scroll), false if
 * `text` doesn't match any /sys* command.
 *
 * Used by two paths:
 *   1. session_prompt onSubmit wrapper (slots.tsx) — preempts the
 *      LLM call entirely. Most reliable; user types `/sys*` +
 *      Enter, we never tell opencode to submit.
 *   2. message.updated event interceptor (registerSysCommands
 *      below) — fallback when (1) didn't apply (e.g. opencode
 *      submitted independently). Aborts the LLM call after-the-
 *      fact and runs the action.
 *
 * Both paths share the same dispatcher to keep behaviour
 * consistent and avoid double-handling logic drift. */
export function dispatchSysCommand(api: any, text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/sys")) return false;

  const sessionID = activeSessionID(api);

  // Bare /sys with no args → friendly dialog fallback.
  if (trimmed === "/sys") {
    if (sessionID) void api.client?.session?.abort?.({ sessionID });
    promptForSubcommand(api);
    return true;
  }

  // /syscloud → applyCloudFix.
  if (trimmed === "/syscloud") {
    if (sessionID) {
      void api.client?.session?.abort?.({ sessionID });
      const cfg = resolveConfig(getStatus());
      void applyCloudFix(api, cfg, sessionID);
    }
    return true;
  }

  // /sysmenu → buildMenuText + inject.
  if (trimmed === "/sysmenu") {
    if (sessionID) {
      void api.client?.session?.abort?.({ sessionID });
      const body = buildMenuText(api);
      void injectChatMessage(api, sessionID, body);
    }
    return true;
  }

  // /sysmodel <name> → switch + relaunch.
  const modelMatch = trimmed.match(/^\/sysmodel\s+(\S+)/);
  if (modelMatch) {
    if (sessionID) {
      void api.client?.session?.abort?.({ sessionID });
      void runSwitchLocalModel(api, sessionID, modelMatch[1]);
    }
    return true;
  }

  // /sysapply <numbers> → fire cached proposals.
  const applyMatch = trimmed.match(/^\/sysapply\s+(.+)/);
  if (applyMatch) {
    if (sessionID) {
      void api.client?.session?.abort?.({ sessionID });
      const ids = parseProposalNumbers(applyMatch[1]);
      const cached = getCachedProposals(sessionID);
      if (cached.length === 0) {
        void injectChatMessage(api, sessionID,
          "  [!] No cached proposals for this session.",
        );
      } else {
        const cfg = resolveConfig(getStatus());
        void (async () => {
          for (const id of ids) {
            const p = cached.find((q) => q.id === id);
            if (!p) {
              await injectChatMessage(api, sessionID,
                `  [!] No proposal #${id} in this session's cache.`,
              );
              continue;
            }
            await applyProposal(api, cfg, sessionID, p);
          }
        })();
      }
    }
    return true;
  }

  // Scroll slashes — match all three spellings:
  //   /sysscrollup /sysscrolldn /sysscrollpgup /sysscrollpgdn
  //     ← canonical (registered with opencode's slash registry)
  //   /sysup /sysdn /syspgup /syspgdn
  //     ← short alias from the iter4 rename, kept for muscle memory
  //   /sysscroll-up /sysscroll-down /sysscroll-pgup /sysscroll-pgdn
  //     ← legacy hyphenated form (typed-manual path only — opencode
  //        rejects hyphens at the registry layer)
  // dn / down both accepted in every form.
  const scrollMatch =
    trimmed.match(/^\/sysscroll(up|dn|down|pgup|pgdn)(?:\s+(\d+))?\s*$/) ||
    trimmed.match(/^\/sys(up|dn|down|pgup|pgdn)(?:\s+(\d+))?\s*$/) ||
    trimmed.match(/^\/sysscroll-(up|down|pgup|pgdn)(?:\s+(\d+))?\s*$/);
  if (scrollMatch) {
    if (sessionID) void api.client?.session?.abort?.({ sessionID });
    const dir = scrollMatch[1];
    const argN = scrollMatch[2] ? parseInt(scrollMatch[2], 10) : NaN;
    const isPg = (dir === "pgup" || dir === "pgdn");
    const rows = Number.isFinite(argN) && argN > 0 ? argN : (isPg ? 10 : 1);
    const direction: -1 | 1 = (dir === "up" || dir === "pgup") ? -1 : 1;
    scrollSidebar(direction * rows, "step");
    return true;
  }

  // matchSysMessage covers /sysdoctor /sysstats /sysmodels /sysrecent
  // /sysreclaim — single-shot CLI subcommands that inject output.
  const m = matchSysMessage(trimmed);
  if (m) {
    if (sessionID) {
      void api.client?.session?.abort?.({ sessionID });
      void runAndInject(api, m.args, m.label);
    }
    return true;
  }

  return false;
}

/** Two-stage TAB helper for destructive/state-changing commands.
 * First TAB (bare slash, no trailing space) → populate prompt
 * with `<slash> ` and surface a confirmation toast. Second TAB
 * (trailing space) → invoke `runAction`. Same discriminator as
 * doScrollSlash. Used so /sysc<TAB> doesn't immediately relaunch
 * opencode just because opencode autocomplete unique-matched
 * /syscloud — user gets a chance to bail or pick a different
 * command. */
function confirmThenRun(
  api: any,
  slashName: string,
  toast: { variant: "info" | "warning" | "error"; message: string },
  runAction: () => void,
): void {
  const raw = readPromptInput();
  // Stage 1: no trailing-space confirmation marker → populate
  // and surface the warning. `raw === slashName` is too strict
  // because opencode may fire onSelect before fully expanding
  // a partial slash (e.g. `/sysc`); trailing-space discrimination
  // handles every case the same way.
  if (!raw.endsWith(" ")) {
    setPromptInput(`${slashName} `);
    showToast(api, {
      variant: toast.variant,
      title: slashName,
      message: toast.message,
      duration: 8_000,
    });
    return;
  }
  clearPrompt();
  runAction();
}

/** Probe `ollama list` and surface the pulled model names as a
 * toast hint. Used by /sysmodel's two-stage TAB flow so the user
 * doesn't have to remember model tags — they see what's actually
 * pulled and ready to switch to. Runs async; toast lands when
 * the subprocess returns (~100ms typical). Filters out embedding
 * models (snowflake / nomic / minilm) since those aren't valid
 * chat_model values. */
async function suggestLocalModels(api: any): Promise<void> {
  try {
    const proc = Bun.spawn(["ollama", "list"], {
      stdout: "pipe", stderr: "pipe",
    });
    const out = await new Response(proc.stdout).text();
    const names: string[] = [];
    // First line is a header; skip. Each subsequent line:
    // NAME    ID    SIZE   MODIFIED
    for (const line of out.split("\n").slice(1)) {
      const cols = line.split(/\s+/);
      const name = cols[0];
      if (!name) continue;
      const stem = name.split(":")[0].toLowerCase();
      if (stem.startsWith("snowflake") || stem.startsWith("nomic") ||
          stem.includes("minilm")) {
        continue;
      }
      names.push(name);
    }
    if (names.length === 0) return;
    showToast(api, {
      variant: "info",
      title: "/sysmodel — available models",
      message: names.join("  ·  "),
      duration: 12_000,
    });
  } catch {
    // best-effort; no toast on probe failure.
  }
}

/** Diagnostic-toast wrapper around scrollSidebar for slash-driven
 * scroll. Surfaces the before/after scrollTop, scrollHeight, and
 * ref kind so the user can see whether the issue is "no
 * overflow" vs "ref missing" vs "scrollTop didn't move".
 *
 * Toasts every time (rather than once-only) so consecutive slash
 * invocations still report. The toast is short and goes away
 * after 4s, so it shouldn't be intrusive. Will be quieted/removed
 * once we've validated the scroll mechanism end to end. */
/** Slash-driven scroll with optional row-count argument.
 *
 * Two-stage TAB UX (matches /sysmodel and /sysapply):
 *   1. User types `/syssc` then TAB → opencode shows the
 *      autocomplete menu listing /sysscroll-up/down/pgup/pgdn.
 *      User picks one — opencode replaces with full slash.
 *   2. onSelect fires with prompt = exact slash, no trailing
 *      space. We populate to `<slash> ` (with trailing space)
 *      and surface a hint toast. NO scroll happens yet.
 *   3. User optionally types a row count.
 *   4. User presses TAB or Enter:
 *      - TAB → onSelect fires again, sees trailing space and/or
 *        digits, executes.
 *      - Enter → opencode submits, message.updated interceptor
 *        catches and executes (LLM call fires briefly, then
 *        aborts).
 *
 * Discriminator: prompt ends with the bare slash and no trailing
 * space → first TAB → populate. Anything else (trailing space,
 * digits, etc.) → execute. */
function doScrollSlash(
  api: any,
  slashName: string,
  direction: -1 | 1,
  defaultRows: number,
): void {
  const raw = readPromptInput();
  const trimmed = raw.trim();
  const escaped = slashName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = trimmed.match(new RegExp(`^${escaped}(?:\\s+(\\d+))?\\s*$`));
  const hasDigitArg = !!(m && m[1]);
  const hasTrailingSpace = raw.endsWith(" ");

  // Stage 1: no digit arg AND no trailing-space confirmation
  // marker → user just picked from autocomplete. Populate the
  // prompt with `<slash> ` so they can either type N + TAB to
  // scroll N rows, or just hit TAB (now sees trailing space) to
  // scroll the default. Earlier discriminator (`trimmed ===
  // slashName`) was too strict — opencode sometimes fires
  // onSelect before fully expanding the prompt, leaving us
  // looking at e.g. `/syssc` partial text. Args/trailing-space
  // discrimination handles that case naturally.
  if (!hasDigitArg && !hasTrailingSpace) {
    setPromptInput(`${slashName} `);
    showToast(api, {
      variant: "info",
      title: slashName,
      message: `Type N then TAB to scroll N rows.  ` +
               `TAB or Enter alone → ${defaultRows} row${defaultRows === 1 ? "" : "s"}.`,
      duration: 6_000,
    });
    return;
  }

  // Stage 2: digit arg or trailing-space marker → execute.
  let rows = defaultRows;
  if (hasDigitArg) {
    const n = parseInt(m![1], 10);
    if (Number.isFinite(n) && n > 0) rows = n;
  }
  clearPrompt();
  scrollSidebar(direction * rows, "step");
}

/** Built-in opencode slash commands. opencode's TUI ships with
 * these regardless of plugin/project config. Hardcoded because
 * (a) we don't have a programmatic way to enumerate them from
 * the SDK and (b) they're stable across opencode versions —
 * a stale entry is way less harmful than no listing at all.
 * Update when opencode adds/removes built-ins. */
const OPENCODE_BUILTIN_SLASHES: Array<[string, string]> = [
  ["/help",    "show help"],
  ["/init",    "initialize a new opencode project"],
  ["/share",   "share the current session"],
  ["/agents",  "switch agent"],
  ["/models",  "switch model"],
  ["/themes",  "switch theme"],
  ["/sessions", "switch session"],
  ["/clear",   "clear chat history"],
  ["/redo",    "redo last command"],
  ["/undo",    "undo last command"],
];

/** Read project slash commands from `.opencode/command/*.md`.
 * Each file is one slash; the file's frontmatter (or first
 * heading, fallback) carries a description. cli.py writes these
 * at launch time per `_opencode_slash_commands`.
 *
 * Returns an array of [slash_name, description] tuples sorted by
 * name. Errors (missing dir, unreadable files) return empty —
 * /sysmenu degrades gracefully to just the hardcoded /sys* +
 * built-ins lists. */
function readProjectSlashes(directory: string): Array<[string, string]> {
  const cmdDir = `${directory}/.opencode/command`;
  let files: string[];
  try {
    files = readdirSync(cmdDir).filter((f) => f.endsWith(".md")).sort();
  } catch {
    return [];
  }
  const out: Array<[string, string]> = [];
  for (const f of files) {
    const name = f.replace(/\.md$/, "");
    let description = "";
    try {
      const body = readFileSync(`${cmdDir}/${f}`, "utf-8");
      // Prefer YAML frontmatter `description:` field; fall back to
      // first non-empty non-frontmatter line.
      const fmMatch = body.match(/^---\s*\n([\s\S]*?)\n---/);
      if (fmMatch) {
        const dMatch = fmMatch[1].match(/^description:\s*(.+?)\s*$/m);
        if (dMatch) description = dMatch[1].replace(/^["']|["']$/g, "");
      }
      if (!description) {
        const afterFm = body.replace(/^---[\s\S]*?---\s*/, "");
        const firstLine = afterFm.split("\n").find((l) => l.trim()) ?? "";
        description = firstLine.replace(/^#+\s*/, "").trim().slice(0, 80);
      }
    } catch {
      // can't read — leave description empty
    }
    out.push([`/${name}`, description]);
  }
  return out;
}

/** Build the /sysmenu body as multiple LCARS frames — one per
 * section. opencode user messages render markdown literally
 * (verified: role==="assistant" gates the markdown renderer in
 * the binary), so we use box-drawing frames + emoji titles +
 * embedded ANSI for color (best-effort — survives if opencode
 * passes ANSI through, falls back to monochrome otherwise).
 */
function buildMenuText(api: any): string {
  const directory = api.state?.path?.directory ?? process.cwd();
  const project = readProjectSlashes(directory);

  // Group project slashes by family prefix (foo-bar → "foo").
  const families = new Map<string, Array<[string, string]>>();
  for (const [name, desc] of project) {
    const stem = name.replace(/^\//, "");
    const family = stem.includes("-") ? stem.split("-")[0] : "misc";
    if (!families.has(family)) families.set(family, []);
    families.get(family)!.push([name, desc]);
  }

  const pad = (s: string, w: number) => s.length >= w ? s : s + " ".repeat(w - s.length);
  const COL = 22;
  const fmtRow = ([name, desc]: [string, string]) =>
    desc ? `${pad(name, COL)}${desc}` : name;

  const sysCmds: Array<[string, string]> = [
    ["/sys <args>",         "run any org-llm subcommand"],
    ["/sysmenu",            "this listing"],
    ["/sysdoctor",          "system health check"],
    ["/sysstats",           "vault counts"],
    ["/sysmodels",          "list configured models"],
    ["/sysrecent",          "last 7d activity"],
    ["/syscloud",           "apply auto-doctor cloud fix + relaunch"],
    ["/sysreclaim",         "stop unused Ollama models (free RAM)"],
    ["/sysmodel <name>",    "switch local chat_model + relaunch"],
    ["/sysapply <numbers>", "apply cached auto-doctor proposals"],
    ["/sysscrollup [N]",     "scroll sidebar up N rows (default 1)"],
    ["/sysscrolldn [N]",     "scroll sidebar down N rows (default 1)"],
    ["/sysscrollpgup [N]",   "scroll sidebar up N rows (default 10)"],
    ["/sysscrollpgdn [N]",   "scroll sidebar down N rows (default 10)"],
  ];

  const out: string[] = [];

  out.push(...frame("⚡ /sys* — LOCAL SUBPROCESS (no LLM)",
    sysCmds.map(fmtRow), "PRIMARY"));
  out.push("");

  if (families.size > 0) {
    const families_sorted = [...families.entries()].sort(
      (a, b) => a[0].localeCompare(b[0]),
    );
    for (const [family, items] of families_sorted) {
      const sortedItems = items.sort((a, b) => a[0].localeCompare(b[0]));
      out.push(...frame(
        `📁 /${family}-* — PROJECT COMMANDS (${items.length})`,
        sortedItems.map(fmtRow),
        "SECONDARY",
      ));
      out.push("");
    }
  } else {
    out.push(...frame("📁 PROJECT COMMANDS",
      ["(no .opencode/command/*.md files found in this workspace)"],
      "SECONDARY"));
    out.push("");
  }

  out.push(...frame("🛰 OPENCODE BUILT-INS",
    OPENCODE_BUILTIN_SLASHES.map(fmtRow),
    "ACCENT"));

  return out.join("\n");
}

/** Defensive wrapper for a dialog mount. opencode's TUI crashes
 * the whole host if a JSX/string mismatch reaches its renderer
 * (we hit "Orphan text error" when a description callback
 * returned a raw string). Wrapping in try/catch + falling back
 * to a toast keeps the user in the TUI even when a slot or
 * dialog has a bug. */
function safeMountDialog(api: any, factory: () => any, contextLabel: string): void {
  try {
    api.ui?.dialog?.replace?.(factory);
  } catch (e) {
    showToast(api, {
      variant: "error",
      title: contextLabel,
      message: `dialog mount failed: ${(e as Error)?.message ?? "unknown"}`,
      duration: 6_000,
    });
  }
}

/** Open a DialogPrompt that collects free-form CLI args. Used as
 * a fallback when the user types just `/sys` with no args.
 * Normal usage is `/sys <args>` inline — the message-event
 * interceptor in `registerSysCommands` handles that path without
 * any dialog. */
function promptForSubcommand(api: any): void {
  safeMountDialog(api, () =>
    api.ui.DialogPrompt({
      title: "Run an org-llm subcommand",
      description: () => (
        <box flexDirection="column">
          <text>Type EITHER:</text>
          <text>{"  • literal args — e.g. `doctor --power-boost`"}</text>
          <text>{"  • natural language — e.g. `show me recent`"}</text>
          <text> </text>
          <text>{"Or close this dialog and just type"}</text>
          <text>{"`/sys <args>` directly into the prompt."}</text>
        </box>
      ),
      placeholder: "doctor --power-boost  (or: how big is my vault?)",
      onConfirm: (args: string) => {
        api.ui?.dialog?.clear?.();
        const argList = (args ?? "").split(/\s+/).filter((x: string) => x);
        if (argList.length === 0) return;
        void runAndInject(api, argList, `org-llm ${argList.join(" ")}`);
      },
      onCancel: () => api.ui?.dialog?.clear?.(),
    }),
    "/sys"
  );
}

/** Match a user message text against our /sys* slash patterns.
 * Returns `{args, label}` if it matches, null otherwise. The
 * message-event interceptor uses this to decide which messages
 * to handle as CLI invocations vs let through to the LLM. */
function matchSysMessage(text: string): { args: string[]; label: string } | null {
  const t = text.trim();
  if (!t) return null;

  // /sys <args> — anything after /sys + space is the CLI args
  const sysMatch = t.match(/^\/sys\s+(.+)$/s);
  if (sysMatch) {
    const args = sysMatch[1].trim().split(/\s+/);
    return { args, label: `org-llm ${args.join(" ")}` };
  }

  // Predefined wrappers — match exact slash command names
  switch (t) {
    case "/sysdoctor":
      return {
        args: ["doctor", "--power-boost", "--no-diagnose"],
        label: "Doctor — system health",
      };
    case "/sysstats":
      return { args: ["stats"], label: "Vault stats" };
    case "/sysmodels":
      return { args: ["models"], label: "Models" };
    case "/sysrecent":
      return { args: ["discover"], label: "Recent activity" };
    case "/sysreclaim":
      return { args: ["models", "--reclaim"], label: "Reclaim — free RAM" };
    // /syscloud is intentionally NOT in this map — it's not a
    // run-and-inject CLI subcommand, it's a confirm-trigger for
    // the slow-LLM auto-doctor flow. The interceptor handles it
    // separately, calling applyCloudFix directly.
  }
  return null;
}

/** Pull text content out of a Message's parts array. */
function extractMessageText(msg: any): string {
  const parts = Array.isArray(msg?.parts) ? msg.parts : [];
  return parts
    .filter((p: any) => p?.type === "text" && typeof p?.text === "string")
    .map((p: any) => p.text)
    .join("\n");
}

/** Register the org-llm-sys slash commands.
 *
 * UX intent: the user types `/sys` → autocomplete completes the
 * literal `/sys ` text into the prompt → user keeps typing args
 * inline (`/sys doctor --power-boost`) → Enter submits → the
 * message-event interceptor below sees the user's text, parses
 * it, runs the CLI subprocess, and injects the output as a
 * noReply chat message. NO dialog interrupts the flow.
 *
 * Why onSelect is a no-op: opencode fires the command's onSelect
 * when the user picks the entry from the autocomplete menu (i.e.
 * Tab-completes). Doing anything in onSelect (popping a dialog,
 * running a subprocess, etc.) interrupts the user mid-type. The
 * actual command execution happens at SUBMIT time via the
 * message.updated interceptor — that's when the full text
 * (slash + args) is available.
 *
 * The /sys bare case (user submits `/sys` with no args) falls
 * through to the dialog as a friendly fallback, since there's
 * nothing to run.
 */
export function registerSysCommands(api: any): void {
  // Slash commands appear in the autocomplete menu. CRITICAL:
  // onSelect runs the action DIRECTLY and clears the prompt —
  // never calls ref.submit(). Earlier iteration submitted via
  // ref.submit() so the user could just press TAB and the action
  // fired, but submit pipes the slash through opencode's normal
  // submission pipeline, which always invokes the LLM. On slow
  // local Ollama that meant /sysdown (a 1ms scroll!) hung waiting
  // for a multi-second LLM round-trip. Now: action fires inline,
  // prompt clears, no LLM contact.
  //
  // For arg-bearing slashes (/sysmodel, /sysapply): the user types
  // "/sysapply 1" then TAB; onSelect reads the prompt content via
  // readPromptInput(), parses args, runs the action, clears the
  // prompt. Same no-LLM contract.
  //
  // The message.updated interceptor below stays as a fallback for
  // users who type the slash + Enter without TAB-selecting first.
  // In that path opencode WILL fire the LLM call (we can't stop
  // it from a plugin) — the interceptor aborts as fast as it can.
  // TAB-select is the recommended path; it's strictly faster.
  api.command?.register?.(() => [
    {
      // Phase 18.4-iter5: renamed from `/sys` → `/sysrun` because
      // the bare `sys` slash name was prefix-shadowing every
      // longer /sys* slash (sysdn, sysup, syspgup, syspgdn) in
      // opencode's slash router. User types /sysdn → router finds
      // `sys` first (shorter prefix) → fires the bare onSelect
      // dialog. Dropping the prefix collision entirely.
      // The keypress / message.updated hooks still match bare
      // `/sys` typed text via dispatchSysCommand for users with
      // muscle memory.
      title: "/sysrun <args> — run any org-llm subcommand (no LLM)",
      value: "org-llm.sysrun",
      description: "Type /sysrun followed by CLI args, then TAB. Output injects into chat.",
      category: "org-llm",
      slash: { name: "sysrun", aliases: [] },
      onSelect: () => {
        const text = readPromptInput().trim();
        const m = text.match(/^\/sys(?:run)?\s+(.+)$/);
        if (!m) {
          setPromptInput("/sysrun ");
          return;
        }
        clearPrompt();
        const argList = m[1].split(/\s+/).filter(Boolean);
        void runAndInject(api, argList, `org-llm ${argList.join(" ")}`);
      },
    },
    {
      title: "Doctor — system health (no LLM)",
      value: "org-llm.sysdoctor",
      description: "TAB-select to run `org-llm doctor` and inject output into chat.",
      category: "org-llm",
      slash: { name: "sysdoctor" },
      onSelect: () => {
        clearPrompt();
        void runAndInject(api,
          ["doctor", "--power-boost", "--no-diagnose"],
          "Doctor — system health");
      },
    },
    {
      title: "Stats — vault counts (no LLM)",
      value: "org-llm.sysstats",
      description: "TAB-select to inject `org-llm stats` output into chat.",
      category: "org-llm",
      slash: { name: "sysstats" },
      onSelect: () => {
        clearPrompt();
        void runAndInject(api, ["stats"], "Vault stats");
      },
    },
    {
      title: "Models — list configured models (no LLM)",
      value: "org-llm.sysmodels",
      description: "TAB-select to inject `org-llm models` output into chat.",
      category: "org-llm",
      slash: { name: "sysmodels" },
      onSelect: () => {
        clearPrompt();
        void runAndInject(api, ["models"], "Models");
      },
    },
    {
      title: "Recent — last 7d activity (no LLM)",
      value: "org-llm.sysrecent",
      description: "TAB-select to inject `org-llm discover` output into chat.",
      category: "org-llm",
      slash: { name: "sysrecent" },
      onSelect: () => {
        clearPrompt();
        void runAndInject(api, ["discover"], "Recent activity");
      },
    },
    {
      title: "Cloud — apply slow-LLM doctor fix (relaunch via cloud)",
      value: "org-llm.syscloud",
      description: "Confirm-trigger for the auto-doctor: switches to cloud and relaunches.",
      category: "org-llm",
      slash: { name: "syscloud" },
      onSelect: () => confirmThenRun(api, "/syscloud",
        { variant: "warning",
          message: "Will RELAUNCH opencode in cloud mode (kills current session). TAB or Enter to confirm." },
        () => {
          const sessionID = activeSessionID(api);
          const cfg = resolveConfig(getStatus());
          if (sessionID) void applyCloudFix(api, cfg, sessionID);
        }),
    },
    {
      title: "Menu — list all slash commands (no LLM)",
      value: "org-llm.sysmenu",
      description: "Hardcoded /sys* + project commands + opencode built-ins. Zero LLM cost.",
      category: "org-llm",
      slash: { name: "sysmenu" },
      onSelect: () => {
        clearPrompt();
        const sessionID = activeSessionID(api);
        if (!sessionID) return;
        const body = buildMenuText(api);
        void injectChatMessage(api, sessionID, body);
      },
    },
    {
      title: "Model — switch local chat_model + relaunch",
      value: "org-llm.sysmodel",
      description: "Type /sysmodel <name> then TAB (e.g. /sysmodel llama3.2:1b).",
      category: "org-llm",
      slash: { name: "sysmodel" },
      onSelect: () => {
        const text = readPromptInput().trim();
        const m = text.match(/^\/sysmodel\s+(\S+)/);
        if (!m) {
          setPromptInput("/sysmodel ");
          // Auto-suggest available local models. Spawned async so
          // we don't block onSelect; toast lands after `ollama
          // list` returns (~100ms typical). User picks a name and
          // types it, then TABs again to apply.
          void suggestLocalModels(api);
          return;
        }
        clearPrompt();
        const sessionID = activeSessionID(api);
        if (sessionID) void runSwitchLocalModel(api, sessionID, m[1]);
      },
    },
    {
      title: "Apply — fire one or more cached auto-doctor proposals",
      value: "org-llm.sysapply",
      description: "Type /sysapply 1,3 then TAB to apply cached proposals.",
      category: "org-llm",
      slash: { name: "sysapply" },
      onSelect: () => {
        const text = readPromptInput().trim();
        const m = text.match(/^\/sysapply\s+(.+)/);
        if (!m) {
          setPromptInput("/sysapply ");
          // Auto-suggest the cached proposal IDs for THIS session
          // so the user sees what's actually applicable.
          const sid = activeSessionID(api);
          const cached = sid ? getCachedProposals(sid) : [];
          if (cached.length === 0) {
            showToast(api, {
              variant: "warning",
              title: "/sysapply",
              message: "No cached proposals. Wait for the next auto-doctor pass.",
              duration: 6_000,
            });
          } else {
            const opts = cached
              .filter((p) => p.action.kind !== "info")
              .map((p) => `${p.id}=${p.heading}`)
              .join("  ·  ");
            showToast(api, {
              variant: "info",
              title: "/sysapply",
              message: `Pick numbers (comma/range): ${opts}`,
              duration: 12_000,
            });
          }
          return;
        }
        clearPrompt();
        const sessionID = activeSessionID(api);
        if (!sessionID) return;
        const ids = parseProposalNumbers(m[1]);
        const cached = getCachedProposals(sessionID);
        if (cached.length === 0) {
          void injectChatMessage(api, sessionID,
            "⚠  No cached proposals for this session. Wait for the next auto-doctor pass and try again.",
          );
          return;
        }
        const cfg = resolveConfig(getStatus());
        void (async () => {
          for (const id of ids) {
            const p = cached.find((q) => q.id === id);
            if (!p) {
              await injectChatMessage(api, sessionID,
                `  [!] No proposal #${id} in this session's cache.`,
              );
              continue;
            }
            await applyProposal(api, cfg, sessionID, p);
          }
        })();
      },
    },
    {
      title: "Reclaim — stop unused Ollama models (free RAM)",
      value: "org-llm.sysreclaim",
      description: "Stops loaded Ollama models not assigned to a role.",
      category: "org-llm",
      slash: { name: "sysreclaim" },
      onSelect: () => confirmThenRun(api, "/sysreclaim",
        { variant: "info",
          message: "Stops Ollama models not assigned to a role. TAB or Enter to confirm." },
        () => void runAndInject(api, ["models", "--reclaim"], "Reclaim — free RAM")),
    },
    // Phase 18.4-iter4: opencode's slash registry rejects hyphens
    // ("Unknown command: /sysscroll-up" when injected via Doom's
    // vterm-send-string). Canonical names are hyphen-free but keep
    // "scroll" in them so the autocomplete reads as scroll commands,
    // not opaque /sysup / /sysdn pairs. Legacy hyphenated forms
    // continue to work when typed manually because dispatchSysCommand
    // below matches all spellings.
    {
      title: "Sidebar scroll up — N rows (default 1)",
      value: "org-llm.sysscrollup",
      description: "Type /sysscrollup [N] then TAB. Default scrolls 1 row.",
      category: "org-llm",
      slash: { name: "sysscrollup" },
      onSelect: () => doScrollSlash(api, "/sysscrollup", -1, 1),
    },
    {
      title: "Sidebar scroll down — N rows (default 1)",
      value: "org-llm.sysscrolldn",
      description: "Type /sysscrolldn [N] then TAB. Default scrolls 1 row.",
      category: "org-llm",
      slash: { name: "sysscrolldn" },
      onSelect: () => doScrollSlash(api, "/sysscrolldn", +1, 1),
    },
    {
      title: "Sidebar scroll page up — N rows (default 10)",
      value: "org-llm.sysscrollpgup",
      description: "Type /sysscrollpgup [N] then TAB. Default scrolls 10 rows.",
      category: "org-llm",
      slash: { name: "sysscrollpgup" },
      onSelect: () => doScrollSlash(api, "/sysscrollpgup", -1, 10),
    },
    {
      title: "Sidebar scroll page down — N rows (default 10)",
      value: "org-llm.sysscrollpgdn",
      description: "Type /sysscrollpgdn [N] then TAB. Default scrolls 10 rows.",
      category: "org-llm",
      slash: { name: "sysscrollpgdn" },
      onSelect: () => doScrollSlash(api, "/sysscrollpgdn", +1, 10),
    },
  ]);

  // Submit-time interceptor for /sys* commands. The HTTP proxy
  // (org_llm/llm_proxy.py) returns the LLM-side response cleanly
  // for these — empty assistant turn marked "✓ handled locally".
  // This event handler runs in PARALLEL: dispatches the actual
  // sys action (sidebar scroll, chat injection, etc.) so the
  // user sees their action take effect alongside the assistant
  // marker.
  //
  // Earlier iteration also called session.deleteMessage and
  // session.abort here. Both removed:
  //   • deleteMessage caused opencode's session.processor to
  //     hang for 5 minutes (it was in the middle of processing
  //     the very message we deleted).
  //   • abort was redundant once the proxy started returning a
  //     proper SSE-with-finish-reason response — opencode's loop
  //     ends naturally on receiving stop.
  api.event?.on?.("message.updated", (e: any) => {
    try {
      const msg = e?.properties?.info;
      if (msg?.role !== "user") return;
      const text = extractMessageText(msg);
      if (!text.trim().startsWith("/sys")) return;
      dispatchSysCommand(api, text);
    } catch (err) {
      showToast(api, {
        variant: "error",
        title: "/sys interceptor",
        message: `error handling message: ${(err as Error)?.message ?? "unknown"}`,
        duration: 5_000,
      });
    }
  });
}
