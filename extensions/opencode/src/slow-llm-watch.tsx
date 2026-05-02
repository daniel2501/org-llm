// @ts-nocheck
//
// Same JSX-runtime / @ts-nocheck rationale as the rest of the
// plugin — see slots.tsx for the full explanation.
//
/**
 * @org-llm/opencode-plugin — slow-LLM watcher (Phase 17.1k).
 *
 * Watches from outside the LLM loop:
 *
 *   1. message.updated event with role=user → start stopwatch.
 *   2. message.part.updated with type="text" → AI is responding,
 *      cancel the stopwatch.
 *   3. Periodic poll (5s tick) — if stopwatch still running past
 *      `cfg.slow_llm_threshold_ms` (default 25 s), kick the
 *      auto-doctor flow.
 *
 * Auto-doctor flow (one-shot per stuck round):
 *   a. Abort the stuck LLM call via api.client.session.abort.
 *   b. Run `org-llm doctor --power-boost --no-diagnose` via
 *      subprocess. Capture stdout, strip ANSI.
 *   c. Inject the doctor output into chat as a noReply message —
 *      becomes a permanent record in the session, not a transient
 *      toast. (See sys-commands.tsx for the helpers.)
 *   d. Auto-apply the cloud-routing fix:
 *      `org-llm config sidebar_auto_session_use_cloud true`.
 *      Inject a follow-up confirmation message.
 *   e. The user can then exit + relaunch to use cloud, OR keep
 *      working with local now that the diagnostic is in chat.
 */

import type { SidebarConfig } from "./panel";
import { getStatus, showToast, getActiveModelOverride } from "./panel";
import {
  runOrgLlm, runAndInject, runSwitchLocalModel,
  injectChatMessage, activeSessionID, frame,
} from "./sys-commands";

/** A single auto-doctor proposal — one row the user can apply via
 * `/sysapply <number>`. Built in priority order in
 * runAutoDoctorFlow, cached per-session, rendered as a numbered
 * markdown list. The `action` kind is the dispatch tag for
 * applyProposal: kept as a discriminated union so adding a new
 * action requires updating both the builder and the dispatcher,
 * caught by the TS exhaustiveness check (despite @ts-nocheck on
 * the module — the union forces a single switch site). */
export type ProposalAction =
  | { kind: "reclaim" }
  | { kind: "cloud" }
  | { kind: "sysmodel"; model: string }
  | { kind: "keep-alive"; value: string }   // proxy_prompt_keep_alive
  | { kind: "info" };          // surfaced but not auto-applicable

export interface Proposal {
  id: number;                  // 1-indexed
  emoji: string;               // section glyph
  heading: string;             // section heading text
  bullets: string[];           // body bullets — markdown list
  slashHint: string;           // suggested manual slash, e.g. "/syscloud"
  action: ProposalAction;
}

// Per-session proposal cache. /sysapply reads from this when the
// user picks a number. Cleared when the session ID rotates (a new
// auto-doctor pass replaces the cache for that session).
const _proposalsBySession = new Map<string, Proposal[]>();

export function getCachedProposals(sessionID: string): Proposal[] {
  return _proposalsBySession.get(sessionID) ?? [];
}

/** Apply a numbered proposal. Routes to the right helper based
 * on action kind. Used by /sysapply (sys-commands.tsx).
 *
 * Kept here rather than in sys-commands.tsx because the action
 * union type is defined here (avoids a circular import). The
 * actual helpers it calls (applyCloudFix, runSwitchLocalModel,
 * runAndInject) live in their own modules. */
export async function applyProposal(
  api: any,
  cfg: Required<SidebarConfig>,
  sessionID: string,
  p: Proposal,
): Promise<void> {
  const a = p.action;
  switch (a.kind) {
    case "reclaim":
      await runAndInject(api, ["models", "--reclaim"], "Reclaim — free RAM");
      return;
    case "cloud":
      await applyCloudFix(api, cfg, sessionID);
      return;
    case "sysmodel":
      await runSwitchLocalModel(api, sessionID, a.model);
      return;
    case "keep-alive":
      // Persist proxy_prompt_keep_alive (read by intercept_prompt_cache
      // in org_llm/llm_proxy.py) — the proxy injects this value into
      // every local-shape /chat/completions request, telling Ollama to
      // keep the model warm between commands. No shell-rc surgery
      // required; the value lives in org-llm config and survives
      // restarts. Note: this affects opencode-mediated traffic.
      // Direct `ollama run` calls still need OLLAMA_KEEP_ALIVE in
      // shell env — but those aren't org-llm's concern.
      try {
        const proc = Bun.spawn(
          ["org-llm", "config", "proxy_prompt_keep_alive", a.value],
          { stdout: "pipe", stderr: "pipe" },
        );
        await proc.exited;
        const ok = proc.exitCode === 0;
        await injectChatMessage(api, sessionID,
          ok
            ? `  [✓] Persisted proxy_prompt_keep_alive = ${a.value}.  ` +
              `The proxy will inject this on every local LLM request — ` +
              `Ollama keeps the model warm between commands.`
            : `  [✗] Failed to persist proxy_prompt_keep_alive. ` +
              `Run manually:  org-llm config proxy_prompt_keep_alive ${a.value}`,
        );
      } catch (err) {
        await injectChatMessage(api, sessionID,
          `  [✗] keep-alive persist threw: ${(err as Error)?.message ?? "unknown"}`,
        );
      }
      return;
    case "info":
      await injectChatMessage(api, sessionID,
        `  [i] ${p.slashHint} is informational — no auto-apply available. Read the proposal text and act manually.`,
      );
      return;
  }
}

const POLL_INTERVAL_MS = 5_000;

/** Built-in opencode slash commands that round-trip through the
 * LLM. When one of these tripped the slow-LLM threshold we can
 * suggest a no-LLM `/sys*` alternative as the quick-win fix. */
const LLM_HEAVY_SLASHES = new Set<string>([
  "/menu", "/help", "/init", "/agents", "/models", "/themes",
  "/sessions", "/clear", "/share", "/redo", "/undo",
]);

/** If the stuck user message is a slash command opencode handles
 * via the LLM, return that slash so the auto-doctor can suggest
 * the local /sys* equivalent. Returns "" otherwise. */
function detectLLMHeavySlash(text: string): string {
  const trimmed = (text ?? "").trim();
  if (!trimmed.startsWith("/")) return "";
  // Match the slash + first word only (ignore args).
  const m = trimmed.match(/^(\/[a-z-]+)/i);
  if (!m) return "";
  return LLM_HEAVY_SLASHES.has(m[1].toLowerCase()) ? m[1] : "";
}

/** Process names we never recommend closing because they ARE the
 * user's working environment — closing them would lose work.
 * Matched as a PREFIX against the process name (so "emacs"
 * matches ".emacs-30.2-real-name", common when distros prefix
 * the binary with a dot or version suffix). The top-RAM probe
 * filters these out so we only surface processes that are
 * actually dispensable (browsers, music apps, etc.). */
const KEEP_RUNNING_PREFIXES = [
  // Inference + this app
  "ollama", "org-llm", "opencode", "claude", "bun",
  // Editor / shell / terminal (.emacs-30.2-rea / nvim / etc.)
  "emacs", ".emacs", "nvim", "vim", "code", "vscode",
  "kitty", "alacritty", "wezterm", "ghostty",
  "tmux", "screen", "fish", "bash", "zsh", "sh", "dash",
  // OS infra
  "systemd", "sshd", "Xorg", "Xwayland", "wayland", "wayfire",
  "kwin", "plasmashell", "gnome-shell", "pulseaudio", "pipewire",
  "NetworkManager", "dbus-daemon", "polkitd", "udevd",
];

/** Process-name patterns that ARE "memory hogs the user can
 * usually close" — browsers, chat apps, music. Substring match
 * (case-insensitive). Includes browser RENDERER process names
 * (QtWebEngineProc, chrome_crashpad, etc.) so the entries actually
 * surface — those typically dwarf the parent browser's RSS.
 *
 * Note we still summarise to ONE entry per matching app: the
 * dedupe in topRAMConsumers keys on the base name, so 14 copies
 * of QtWebEngineProc collapse to a single line listing the
 * largest. Closing the parent browser kills all of them. */
const DISPENSABLE_PATTERNS = [
  // Browsers (parent processes)
  "firefox", "chrome", "chromium", "qutebrowser", "brave",
  "safari", "opera", "vivaldi", "tor-browser",
  // Browser render/helper processes (often dwarf the parent)
  "qtwebengine", "chrome_crashpad", "chrome-sandbox",
  "firefox-content", "renderer",
  // Chat apps
  "slack", "discord", "telegram", "signal", "element",
  // Media
  "spotify", "vlc", "rhythmbox", "audacious",
  // Heavy meeting / streaming
  "zoom", "teams", "obs",
];

/** Probe top RAM-consuming processes via `ps`. Returns the top
 * 3 dispensable processes (≥200 MB RSS each) sorted by RAM,
 * filtered to exclude the user's working toolchain. Empty array
 * on probe failure. The list lives in this module rather than a
 * config knob because it's stable across user setups — adding
 * specific apps would need restart anyway. */
async function topRAMConsumers(): Promise<Array<{name: string; rssMB: number; dispensable: boolean}>> {
  let out: string;
  try {
    const proc = Bun.spawn(
      ["ps", "-eo", "comm,rss", "--sort=-rss", "--no-headers"],
      { stdout: "pipe", stderr: "pipe" },
    );
    out = await new Response(proc.stdout).text();
  } catch {
    return [];
  }

  const result: Array<{name: string; rssMB: number; dispensable: boolean}> = [];
  const seen = new Set<string>();
  for (const line of out.split("\n")) {
    const m = line.trim().match(/^(\S+)\s+(\d+)$/);
    if (!m) continue;
    const rawName = m[1];
    const baseName = rawName.split("/").pop() ?? rawName;
    const lowName  = baseName.toLowerCase();
    // Prefix match against the keep list — handles dotfile-prefixed
    // and version-suffixed binaries (".emacs-30.2-real-name" etc.).
    if (KEEP_RUNNING_PREFIXES.some((p) => lowName.startsWith(p.toLowerCase()))) {
      continue;
    }
    const rssMB = parseInt(m[2], 10) / 1024;
    if (rssMB < 200) break;  // sorted desc; below threshold = stop scanning
    // Dedupe by base name — multi-process apps (browsers!) show
    // up many times; we only want one entry summarising peak.
    if (seen.has(baseName)) continue;
    seen.add(baseName);
    const dispensable = DISPENSABLE_PATTERNS.some(
      (p) => lowName.includes(p),
    );
    result.push({ name: baseName, rssMB, dispensable });
    if (result.length >= 5) break;
  }
  return result;
}

/** Parse `org-llm doctor --power-boost --no-diagnose` output to
 * extract the action verb (ok/upsize/downsize/cloud/manual) and
 * any model name the doctor recommends switching to. The doctor
 * formats lines like:
 *   power-boost: upsize
 *   gemma3 (3.3 GB) fits AND is larger than current llama3.2…
 * The stripAnsi pass in runOrgLlm has already removed color
 * codes; we just match plain text. */
function parseDoctorAction(output: string): {
  action: string;
  recommendedModel: string;
} {
  const cleaned = (output ?? "").replace(/\s+/g, " ");
  const actionMatch = cleaned.match(/power-boost:\s*(\w+)/i);
  const action = actionMatch?.[1]?.toLowerCase() ?? "";

  // Recommended model only meaningful for upsize/downsize. The
  // doctor's panel body starts with `<model> (<size> GB) ...`.
  let recommendedModel = "";
  if (action === "upsize" || action === "downsize") {
    const modelMatch = cleaned.match(/([\w.:-]+)\s*\(\d+(?:\.\d+)?\s*GB\)/);
    recommendedModel = modelMatch?.[1] ?? "";
  }
  return { action, recommendedModel };
}

let _lastUserMessageAt = 0;
// Last user message ID we armed the watcher for. Used to dedupe
// the multiple `message.updated` events opencode fires per user
// message (creation → summary attach → metadata refresh) so
// post-response re-arms don't trigger a phantom auto-doctor.
let _armedForUserMsgID = "";
let _lastUserMessageText = "";
let _aiResponding = false;
let _toastedThisRound = false;

/** Pull the concatenated text content out of a Message object's
 * `parts` array. opencode stores user input as zero-or-more
 * TextPart entries; we join them so the resulting string is
 * exactly what the user typed. */
function extractUserText(msg: any): string {
  const parts = Array.isArray(msg?.parts) ? msg.parts : [];
  const texts = parts
    .filter((p: any) => p?.type === "text" && typeof p?.text === "string")
    .map((p: any) => p.text);
  return texts.join("\n");
}

/** Path where slow-llm-watch stashes the in-flight prompt before
 * applying the cloud-routing fix. cli.py reads this on the next
 * launch, surfaces it as cfg.auto_session_pending_prompt, and the
 * plugin resubmits it. */
function pendingPromptPath(): string {
  const home = process.env.HOME ?? "";
  return `${home}/.local/share/org-llm/pending-prompt.txt`;
}

export function registerSlowLLMWatch(
  api: any,
  cfg: Required<SidebarConfig>,
): void {
  if (!cfg.slow_llm_threshold_ms || cfg.slow_llm_threshold_ms <= 0) return;
  const thresholdMs = cfg.slow_llm_threshold_ms;

  const offMsg = api.event?.on?.("message.updated", (e: any) => {
    const msg = e?.properties?.info;
    // Phase 18.4-iter20: assistant message updates are evidence of
    // streaming progress — disarm the stopwatch.
    if (msg?.role && msg.role !== "user") {
      _aiResponding = true;
      return;
    }
    if (msg?.role !== "user") return;
    const text = extractUserText(msg);
    // ALWAYS update the cached text so the poll's safety check
    // (below) can detect /sys content even if events arrive out
    // of order.
    _lastUserMessageText = text;

    // Phase 18.6: dedupe by message ID. opencode fires
    // `message.updated` repeatedly for the SAME user message —
    // first on creation (metadata only), then again when the
    // assistant turn lands and the user message gets a `summary`
    // diff attached, then again on subsequent metadata changes.
    // Earlier this code reset `_aiResponding=false` and bumped
    // `_lastUserMessageAt` on every fire, which re-armed the
    // watcher AFTER the LLM had already responded — so the
    // auto-doctor fired against a turn that completed cleanly.
    // Audit log of the case that surfaced this:
    //   13:38:16 cloud_failover_no_tools 200 9154ms — real reply
    //   13:38:25-ish: opencode populates user.summary → fires
    //                  message.updated for the user msg again
    //   13:38:25+45s: poll fires doctor on a turn that's done
    //
    // Fix: only reset arm-state when the user-message ID is NEW.
    // Repeated updates for the same ID just refresh text +
    // return.
    const msgID = typeof msg?.id === "string" ? msg.id : "";
    if (msgID && msgID === _armedForUserMsgID) {
      // Same message, late metadata update. Don't re-arm.
      return;
    }
    _armedForUserMsgID = msgID;
    _aiResponding      = false;
    _toastedThisRound  = false;
    if (text.trim().startsWith("/sys")) {
      // /sys* commands never round-trip the LLM. Disarm.
      _lastUserMessageAt = 0;
    } else {
      _lastUserMessageAt = Date.now();
    }
  });

  // Phase 18.4-iter16: count ANY assistant-produced part type as
  // "AI is responding," not just text. Earlier we only looked for
  // type="text" — that misses reasoning models (DeepSeek-R1's
  // <thinking> tokens arrive as type="reasoning") and tool-call
  // streams (type="tool", "tool-call", etc.). Both produce
  // visible activity that means the LLM is doing real work; both
  // should disarm the stopwatch. User report: deepseek-r1
  // failover responses got interrupted by auto-doctor at 25s
  // even though R1 was actively streaming reasoning the whole time.
  const PROGRESS_PART_TYPES = new Set([
    "text",
    "reasoning", "thinking",      // chain-of-thought (R1, qwq, etc.)
    "tool", "tool-call", "tool-input", "tool-output", "tool-result",
    "step-start", "step-end", "step-finish",  // model lifecycle markers
    "file", "image",              // multimodal output
    "patch",                       // file writes (Phase 18.4-iter20)
  ]);
  const offPart = api.event?.on?.("message.part.updated", (e: any) => {
    const part = e?.properties?.part;
    if (part?.type && PROGRESS_PART_TYPES.has(part.type)) {
      _aiResponding = true;
    }
  });

  const interval = setInterval(() => {
    if (_lastUserMessageAt === 0) return;
    if (_aiResponding) return;
    if (_toastedThisRound) return;
    // Defensive: re-check the latest user message text against
    // the /sys prefix at poll time. If event ordering somehow
    // armed us for a /sys command (e.g. opencode fired updates
    // out of order), this catches it before auto-doctor fires.
    if (_lastUserMessageText.trim().startsWith("/sys")) {
      _lastUserMessageAt = 0;
      return;
    }
    // ALSO disarm if the proxy intercepted ANYTHING recently.
    // The .md-body content of project slashes (e.g. /menu's body
    // "Call list_slash_commands...") doesn't start with /sys but
    // gets intercepted at the HTTP layer in <10ms. The audit-log
    // poller in sidebar.tsx sets this global when an intercept
    // lands; we honour it here. 5-second window is wider than
    // any in-flight proxy roundtrip.
    const recentIntercept = (globalThis as any).__orgllm_proxy_recent_intercept_at ?? 0;
    if (Date.now() - recentIntercept < 5_000) {
      _lastUserMessageAt = 0;
      return;
    }
    const elapsed = Date.now() - _lastUserMessageAt;
    if (elapsed < thresholdMs) return;

    _toastedThisRound = true;
    void runAutoDoctorFlow(api, cfg, elapsed);
  }, POLL_INTERVAL_MS);

  api.lifecycle?.onDispose?.(() => {
    clearInterval(interval);
    offMsg?.();
    offPart?.();
  });
}

/** End-to-end: abort, diagnose, inject, optionally confirm,
 * apply fix. Each step is best-effort — a failure at any one
 * stage doesn't abort the rest.
 *
 * Confirm gate (cfg.slow_llm_confirm, default true):
 *   • true → diagnose, inject the doctor output + a list of the
 *     commands the auto-doctor PROPOSES, and stop. The user has
 *     to type `/syscloud` to actually apply (sys-commands.tsx
 *     intercepts that and calls `applyCloudFix`).
 *   • false → fully autonomous: diagnose AND apply immediately
 *     (the original 17.1k–17.1p behavior, but now with the cfg
 *     bug fixed so the relaunch actually fires).
 *
 * The previous iteration silently swallowed `cfg` because it was
 * an undeclared free variable inside this function — every
 * reference to `cfg.slow_llm_auto_relaunch` blew up at runtime,
 * so the relaunch never fired and the user only ever saw step 3.
 * Now `cfg` is a real parameter.
 */
async function runAutoDoctorFlow(
  api: any,
  cfg: Required<SidebarConfig>,
  elapsedMs: number,
): Promise<void> {
  const sessionID = activeSessionID(api);
  const elapsedSec = Math.round(elapsedMs / 1000);

  // Show a toast so the user sees we're acting (the chat injection
  // can take a few seconds while doctor runs).
  showToast(api, {
    variant: "warning",
    title: "LLM slow",
    message: `Local model thinking ${elapsedSec}s. Auto-doctor engaging…`,
  });

  // 1. DO NOT abort the LLM call here.
  //
  // Earlier iterations called session.abort() to "free the user
  // from waiting." That decision turned out wrong for the common
  // case: cold-start + thermal-throttled inference + tool-heavy
  // prompts can legitimately spend 30-60s in prefill before the
  // first chunk arrives. Aborting at the threshold means the user
  // NEVER got the response they were waiting for, AND the
  // auto-doctor's proposal frame replaces it. They lost work,
  // didn't gain a fix.
  //
  // New contract: auto-doctor SUGGESTS, never INTERRUPTS. We
  // inject the diagnostic + proposal frames as parallel chat
  // messages; the LLM keeps streaming whenever it lands. If the
  // user wants to abandon the in-flight request, they can apply a
  // proposal that explicitly does so (/syscloud, /sysmodel) — that
  // path relaunches and replaces the session anyway.
  //
  // Bonus: removing the abort fixes the "sidebar didn't update on
  // model swap" symptom for cold-start prompts. The override is
  // set on the assistant's first message.updated event; aborting
  // pre-empted that event from ever firing, leaving the sidebar
  // stuck on the launch-time model.

  // Pull the actually-running model from (a) the live override —
  // updated on every assistant message.updated, so it reflects
  // mid-session model swaps via opencode's /model picker — falling
  // back to (b) the launch-time sidebar status. The doctor reads
  // `chat_model` from the config DB, which is even more stale, so
  // we pass `--for-model` with our best-known value to make the
  // power-boost analysis target the runtime model rather than the
  // configured one.
  const status = getStatus();
  const ovr = getActiveModelOverride();
  const runningModel = ovr?.model || status?.model?.active || "";
  const provider     = ovr?.provider || status?.model?.provider || "";
  const route        = ovr ? (ovr.provider === "ollama" ? "local" : "cloud")
                              : (status?.model?.route ?? "local");
  const runningDisplay = runningModel || "(unknown)";
  const runningLine  = provider
    ? `${runningDisplay} via ${provider} · ${route}`
    : `${runningDisplay} · ${route}`;

  // 2. Run doctor (subprocess, no LLM call). Pass --for-model so the
  // power-boost analysis targets the running model rather than the
  // raw config row.
  const doctorArgs = ["doctor", "--power-boost", "--no-diagnose"];
  if (runningModel) {
    doctorArgs.push("--for-model", runningModel);
  }
  const doctorOut = await runOrgLlm(doctorArgs);

  // Pull thermal status + doctor action so the proposal can be
  // tailored. Three signals drive the recommendations:
  //   • thermal alert/critical → "wait for cooldown" (ranked top
  //     because no model swap helps a throttling CPU)
  //   • doctor action upsize/downsize/cloud → swap or cloud
  //   • last user msg was an LLM-driven slash like /menu → suggest
  //     /sysmenu as the no-LLM alternative
  const thermal = (status?.vitals ?? []).find((v: any) => v?.name === "thermal");
  const thermalHot = thermal?.status === "alert" || thermal?.status === "critical";
  const doctorAction = parseDoctorAction(doctorOut);
  const heavyLLMSlash = detectLLMHeavySlash(_lastUserMessageText);
  const ramHogs = await topRAMConsumers();
  const dispensableHogs = ramHogs.filter((p) => p.dispensable);

  // 3. Inject the diagnostic into chat. Plain-text formatting
  // because opencode user messages don't render markdown — see
  // sys-commands.tsx buildMenuText for the rationale.
  if (sessionID) {
    const body: string[] = [`Running:  ${runningLine}`];
    if (thermalHot) {
      body.push(`Thermal:  ${thermal?.label ?? "alert"}  (CPU throttling likely)`);
    }
    body.push("");
    body.push("Doctor diagnostic (analyzed for the running model):");
    body.push("");
    for (const line of doctorOut.slice(0, 3_000).split("\n")) {
      body.push(`  ${line}`);
    }
    const framed = frame(
      `🩺 AUTO-DOCTOR  (LLM stuck ${elapsedSec}s)`,
      body,
      thermalHot ? "WARNING" : "PRIMARY",
    );
    await injectChatMessage(api, sessionID, framed.join("\n"));
  } else {
    // Force-pin: this is the diagnostic the user needs to read,
    // and it's the only place they see it (no chat session yet).
    showToast(api, {
      variant: "warning",
      title:   "Doctor diagnosis",
      message: doctorOut.slice(0, 400),
      pin:     true,
    });
  }

  // 4. Stash the stuck prompt regardless of confirm vs auto path —
  // either way it'll be resubmitted on the next launch (if the
  // user types /syscloud now, OR after they relaunch manually).
  if (_lastUserMessageText.trim()) {
    try {
      await Bun.write(pendingPromptPath(), _lastUserMessageText);
    } catch {
      // best-effort
    }
  }

  // 5. Confirm gate. When true, propose context-aware commands —
  // the user types one to apply, OR `/sysapply <numbers>` to
  // batch-apply. Proposal ordering reflects root-cause priority:
  // reclaim is cheapest; thermal-hot → no model swap helps;
  // smaller local model → cheap fix; cloud → last resort.
  if (cfg.slow_llm_confirm) {
    if (sessionID) {
      const proposals: Proposal[] = [];
      let nextId = 1;
      const num = (): number => nextId++;

      // Reclaim — always #1, idempotent, sub-second.
      proposals.push({
        id: num(),
        emoji: "🧹",
        heading: "Reclaim RAM (cheapest fix)",
        bullets: [
          "Stops Ollama models loaded from previous chats / model swaps that aren't assigned to a role.",
          "Often resolves stuck-thinking on ≤16 GB hardware.",
        ],
        slashHint: "/sysreclaim",
        action: { kind: "reclaim" },
      });

      // RAM hogs — informational only. Can't kill user processes
      // ourselves; surface them so the user knows what to close.
      if (dispensableHogs.length > 0) {
        proposals.push({
          id: num(),
          emoji: "💾",
          heading: "Close a RAM hog",
          bullets: [
            "Detected high-RAM apps usually safe to close:",
            ...dispensableHogs.map((p) => `**\`${p.name}\`** — ${p.rssMB.toFixed(0)} MB`),
            "Closing one frees more RAM than any model swap.",
          ],
          slashHint: "(no slash — close manually)",
          action: { kind: "info" },
        });
      } else if (ramHogs.length > 0) {
        proposals.push({
          id: num(),
          emoji: "📊",
          heading: "Top RAM consumers (informational)",
          bullets: ramHogs.slice(0, 3).map(
            (p) => `\`${p.name}\` — ${p.rssMB.toFixed(0)} MB`,
          ),
          slashHint: "(no slash — informational)",
          action: { kind: "info" },
        });
      }

      // Heavy LLM slash bypass. /menu /help etc.
      if (heavyLLMSlash) {
        proposals.push({
          id: num(),
          emoji: "⚡",
          heading: `Bypass the LLM for \`${heavyLLMSlash}\``,
          bullets: [
            `\`${heavyLLMSlash}\` round-trips through the LLM. No-LLM equivalents:`,
            "`/sysmenu` — list slash commands (filesystem read)",
            "`/sysstats`, `/sysmodels`, `/sysrecent`, `/sysdoctor`",
          ],
          slashHint: "/sysmenu",
          action: { kind: "info" },
        });
      }

      // Thermal — informational, no auto-apply.
      if (thermalHot) {
        proposals.push({
          id: num(),
          emoji: "🌡",
          heading: `CPU thermal — ${thermal?.label ?? "alert"}`,
          bullets: [
            "Above ~85°C most CPUs throttle clocks 30-50%.",
            "**No model swap helps a throttling CPU** — let it cool, close background tasks, or use cloud.",
          ],
          slashHint: "(close apps + wait)",
          action: { kind: "info" },
        });
      }

      // Local model swap — actionable when doctor recommends.
      if ((doctorAction.action === "upsize" || doctorAction.action === "downsize")
            && doctorAction.recommendedModel) {
        const direction = doctorAction.action === "upsize" ? "Upsize" : "Downsize";
        const arrow     = doctorAction.action === "upsize" ? "📈" : "📉";
        proposals.push({
          id: num(),
          emoji: arrow,
          heading: `${direction} to \`${doctorAction.recommendedModel}\``,
          bullets: [
            doctorAction.action === "upsize"
              ? "A larger local model fits in your free RAM. Better quality, similar speed."
              : "Current model is too big for your free RAM. A smaller fit will be faster.",
          ],
          slashHint: `/sysmodel ${doctorAction.recommendedModel}`,
          action: { kind: "sysmodel", model: doctorAction.recommendedModel },
        });
      }

      // Cloud — always present, last-resort.
      proposals.push({
        id: num(),
        emoji: "☁",
        heading: "Cloud one-shot",
        bullets: [
          "Relaunch via cloud. One-shot — future launches stay local.",
        ],
        slashHint: "/syscloud",
        action: { kind: "cloud" },
      });

      // Perf tuning — applyable via org-llm config. The proxy's
      // intercept_prompt_cache reads `proxy_prompt_keep_alive` and
      // injects it on every local /chat/completions request, so
      // setting it here keeps Ollama from unloading the model
      // between commands without needing the user to edit shell rc.
      proposals.push({
        id: num(),
        emoji: "🔧",
        heading: "Keep model loaded between commands",
        bullets: [
          "Persist proxy_prompt_keep_alive=24h. Avoids Ollama cold-start" +
            " on subsequent commands.",
        ],
        slashHint: "/sysapply " + (proposals.length + 1),
        action: { kind: "keep-alive", value: "24h" },
      });

      // Cache so /sysapply can fire actions by number later.
      _proposalsBySession.set(sessionID, proposals);

      // Build body: numbered proposals + apply hints + footer.
      // Strip leading "/" from slash references — opencode's chat
      // renderer hides lines with multiple slashes (treats them as
      // command-autocomplete pollution and silently drops). Strip
      // markdown emphasis chars too. The footer reminds the user
      // to prepend "/" when typing.
      const stripSlash = (s: string) => s.replace(/(?<![A-Za-z0-9_-])\/(?=sys)/g, "");
      const body: string[] = [];
      for (const p of proposals) {
        body.push(`[${p.id}]  ${p.emoji} ${p.heading.toUpperCase()}`);
        for (const b of p.bullets) {
          const clean = stripSlash(
            b.replace(/\*\*/g, "").replace(/\*/g, "").replace(/`/g, ""),
          );
          body.push(`     ${clean}`);
        }
        if (p.action.kind !== "info") {
          const hint = stripSlash(p.slashHint);
          // De-dup: when slashHint is already "sysapply N" (the
          // keep-alive proposal does this for lack of a dedicated
          // slash), don't render "Apply: sysapply N   or: sysapply N".
          // Compare on the rendered hint (stripSlash already applied).
          const fallback = `sysapply ${p.id}`;
          if (hint === fallback) {
            body.push(`     Apply:  ${fallback}`);
          } else {
            body.push(`     Apply:  ${hint}   or:  ${fallback}`);
          }
        } else {
          body.push(`     ${stripSlash(p.slashHint)}`);
        }
        body.push("");
      }

      const actionableIds = proposals
        .filter((p) => p.action.kind !== "info")
        .map((p) => p.id);
      if (actionableIds.length > 1) {
        body.push(
          `Apply multiple:  sysapply ${actionableIds.join(",")}` +
          `  (or subset, e.g. sysapply ${actionableIds[0]},${actionableIds[actionableIds.length-1]})`,
        );
        body.push("");
      }

      if (_lastUserMessageText.trim()) {
        const stuck = _lastUserMessageText.slice(0, 60) + (_lastUserMessageText.length > 60 ? "…" : "");
        body.push(`Stashed:  '${stuck}'  (will auto-resubmit on next launch)`);
        body.push("");
      }
      body.push("(Prepend / when typing the apply commands above.)");
      body.push("(Set sidebar_slow_llm_confirm=false to auto-apply.)");
      const framed = frame("💡 AUTO-DOCTOR PROPOSALS", body, "ACCENT");
      await injectChatMessage(api, sessionID, framed.join("\n"));
    }
    return;
  }

  // 6. Autonomous path — apply immediately.
  await applyCloudFix(api, cfg, sessionID);
}

/** Relaunch opencode under `--cloud` for one session. Used by both:
 *   • the autonomous slow-LLM auto-doctor path (cfg.slow_llm_confirm=false)
 *   • the user-triggered `/syscloud` slash command (sys-commands.tsx)
 *
 * Exported so sys-commands can call it directly without
 * duplicating the spawn / process.exit / pending-prompt logic.
 *
 * 17.1q-fix: this used to persist `sidebar_auto_session_use_cloud=
 * true` via `org-llm config`, which made every future launch
 * default to cloud — surprising and wrong for a one-time stuck-LLM
 * event. Now the only mechanism is the `--cloud` flag on the
 * immediate relaunch process. After the user closes that opencode
 * instance, future bare `org-llm launch` falls back to the normal
 * local-first default. The user's stuck prompt is still preserved
 * via pending-prompt.txt and resubmitted on the relaunched
 * instance.
 */
export async function applyCloudFix(
  api: any,
  cfg: Required<SidebarConfig>,
  sessionID: string | null,
): Promise<void> {
  // Immediate toast — gives the user feedback before chat injection
  // and relaunch (~3-4s combined). If the relaunch silently fails
  // (process.exit sandboxed, spawn rejected, etc.) the user at
  // least sees we tried.
  showToast(api, {
    variant: "info",
    title:   "Cloud fix",
    message: "Applying… relaunching opencode via --cloud in 3s.",
  });

  // Confirmation message in chat.
  if (sessionID) {
    const body: string[] = [];
    if (_lastUserMessageText.trim()) {
      body.push("✓ Stashed prompt for resubmit on next launch");
    }
    if (cfg.slow_llm_auto_relaunch) {
      body.push("→ org-llm launch --cloud   (relaunching in 3s)");
      body.push("");
      body.push("One-shot: future bare 'org-llm launch' keeps using local.");
    } else {
      body.push("! Auto-relaunch off. Exit opencode and run:");
      body.push("    org-llm launch --cloud");
    }
    const framed = frame("☁ APPLYING CLOUD FIX", body, "PRIMARY");
    await injectChatMessage(api, sessionID, framed.join("\n"));
  }

  // Drop a relaunch marker, then exit. cli.py's launch() loop
  // sees the marker after opencode returns control and `os.execvp`'s
  // itself with `org-llm launch --cloud`. Replacing the process
  // (rather than spawning a detached child) inherits the user's
  // TTY cleanly — no orphaned headless opencode, no broken
  // terminal control. Plugin detaches its concern from process
  // lifecycle; cli.py owns the relaunch.
  if (!cfg.slow_llm_auto_relaunch) return;
  try {
    const home = process.env.HOME ?? "";
    const marker = `${home}/.local/share/org-llm/relaunch-marker.json`;
    await Bun.write(marker, JSON.stringify({ cloud: true }));
  } catch {
    // Marker write failed — fall through to exit anyway. User can
    // manually re-run `org-llm launch --cloud`.
  }
  setTimeout(() => {
    try {
      process.exit(0);
    } catch {
      try {
        process.kill(process.ppid ?? 0, "SIGTERM");
      } catch {
        // Last-ditch — pending-prompt.txt is on disk and the next
        // manual launch will pick it up.
      }
    }
  }, 1500);
}
