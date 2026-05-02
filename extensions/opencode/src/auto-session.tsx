// @ts-nocheck
//
// JSX-runtime / @ts-nocheck rationale identical to slots.tsx /
// panel.tsx — see those modules for the full explanation.
//
/**
 * @org-llm/opencode-plugin — auto-session opener (Phase 17.1e).
 *
 * The full LCARS sidebar (sidebar_content slot) only renders inside
 * opencode's `session` route. On the welcome (`home`) route, the
 * sidebar slot is empty — the user has to submit something to enter
 * a session and see the sidebar.
 *
 * This module makes that automatic. At plugin mount, after opencode
 * has rendered home_prompt and we've captured the Prompt's ref, we
 * populate the prompt with a configured opening message (default
 * "hi") and call the ref's `submit()`. opencode handles session
 * creation + route navigation in response, the sidebar slot mounts,
 * and the user sees the LCARS panel without manual interaction.
 *
 * Configurability:
 *   • `sidebar_auto_session` (bool, default true) — master switch.
 *     Set false to disable entirely; the user types whatever they
 *     want and the sidebar appears once they submit.
 *   • `sidebar_auto_session_prompt` (str, default "hi") — what to
 *     submit. Keep short to minimise intrusion.
 *   • `sidebar_auto_session_use_cloud` (bool, default false) —
 *     whether the auto-prompt is allowed to fire cloud LLM calls.
 *     Enforced launch-side in cli.py (forces local mode for the
 *     whole launch when false). Documented in panel.tsx config
 *     types but not consumed here.
 *
 * Implementation notes:
 *   • slots.tsx wraps the home_prompt slot's `data.ref` callback so
 *     it forwards to opencode AND captures a copy here via
 *     `setPromptRef`. opencode invokes the ref callback when the
 *     Prompt component mounts (some time after our slot.register).
 *   • The auto-submit call in the plugin's `tui` handler polls for
 *     the captured ref to appear (up to 3 seconds). If it never
 *     does — opencode skipped rendering home_prompt for some reason
 *     — we silently no-op rather than throwing.
 *   • Submit is a one-shot: once we've fired it, subsequent calls
 *     are silently dropped. Avoids duplicate submission if opencode
 *     re-mounts the Prompt during a hot reload.
 *   • If the user manages to type something before our auto-submit
 *     fires, we honour their input and skip auto-submit — don't
 *     overwrite what they typed.
 */

import type { SidebarConfig } from "./panel";

let _capturedRef: any = null;
let _autoSubmitted = false;

/** Called by slots.tsx's home_prompt AND session_prompt slots when
 * opencode hands us the Prompt ref. Stored for later use by
 * `maybeAutoSubmit` (auto-session) and `getPromptRef` (slash
 * commands setting prompt input). Both slots forward the same
 * underlying SDK Prompt ref — capturing whichever fires last is
 * fine because opencode tears down the home prompt before mounting
 * the session prompt. */
export function setPromptRef(ref: any): void {
  _capturedRef = ref;
}

/** Returns the currently-captured Prompt ref, or null. Slash
 * commands use this to set the prompt input on TAB-complete
 * (`onSelect`) instead of letting opencode clear the prompt. */
export function getPromptRef(): any {
  return _capturedRef;
}

/** Whether the home_prompt slot should render its Prompt as
 * visible. Returns:
 *   • true when `auto_session` is disabled (always visible)
 *   • false during the auto-session window (prompt hidden so the
 *     user only sees the welcome logo)
 *   • true once the auto-prompt has fired or aborted (so a stuck
 *     user can still type — defensive fallback if opencode doesn't
 *     navigate after submit)
 *
 * The home_prompt slot calls this on every render. Without a Solid
 * signal, the value isn't reactive — opencode's next re-render
 * picks up the new value, which is fine because (a) the slot is
 * unmounted on a successful navigation anyway, and (b) any
 * subsequent re-render (key press, focus change) flips the prompt
 * back on if we're still on home.
 */
export function shouldShowPrompt(cfg: Required<SidebarConfig>): boolean {
  if (!cfg.auto_session) return true;
  return _autoSubmitted;
}

/** Programmatic auto-submit. No-ops cleanly when:
 *   - cfg.auto_session is false
 *   - the prompt ref never gets captured (3s timeout)
 *   - we already auto-submitted once
 *   - the user has already typed something (we honour their input)
 */
/** Phase 17.1i — option A: skip the LLM entirely.
 *
 * Earlier iterations submitted a prompt via the captured Prompt ref,
 * which triggered an LLM round-trip with opencode's full system
 * prompt (vault context + 64 MCP tool definitions ~22 KB). On
 * cold-start local Ollama with a thermally-throttled CPU, this
 * caused 30-60 s "stuck thinking" hangs. Permission-denying
 * subagents (Phase 17.1h, option B) didn't help — opencode appears
 * to pass tool definitions to the model regardless of agent
 * permission rules.
 *
 * Option A bypasses the LLM:
 *   1. `api.client.session.create()` makes an empty session via
 *      the SDK. No messages, no LLM call.
 *   2. `api.route.navigate({type:"session", sessionID})` switches
 *      the TUI from home view to session view. Sidebar_content
 *      slot mounts. User sees the LCARS sidebar instantly.
 *   3. User types their actual question whenever ready — that
 *      first user message goes to the primary `org-llm` agent
 *      with full vault context + tools.
 *
 * Trade-off vs prompt-submit: no greeting line appears in chat.
 * The org-llm-greeter agent is now unused for auto-session (still
 * defined in opencode.json so users can `@org-llm-greeter` it
 * manually if they want). The `auto_session_prompt` config knob
 * still works — if set to a non-empty string, the plugin falls
 * back to prompt-submit semantics for users who explicitly want
 * a greeting message.
 */
export async function maybeAutoSubmit(api: any, cfg: Required<SidebarConfig>): Promise<void> {
  if (!cfg.auto_session) return;
  if (_autoSubmitted) return;

  // Welcome reveal delay — user sees the LCARS logo briefly before
  // the chat surface takes over. The prompt is hidden during this
  // window per slots.tsx's visibility gate.
  if (cfg.auto_session_delay_ms > 0) {
    await new Promise((r) => setTimeout(r, cfg.auto_session_delay_ms));
  }

  // If the user managed to type something during the delay, they've
  // expressed intent — leave them alone, flip the visibility flag so
  // the home prompt is fully usable, and skip the auto-navigate.
  try {
    const current = _capturedRef?.current;
    if (current?.input?.trim()) {
      _autoSubmitted = true;
      return;
    }
  } catch {
    // ref access failed; fall through.
  }

  _autoSubmitted = true;

  try {
    // Create an empty session via the SDK. No LLM round-trip; just
    // a row in opencode's session DB.
    const created: any = await api.client?.session?.create?.({
      title: "org-llm",
    });
    const sessionID =
      created?.data?.id ?? created?.id ?? created?.body?.id;
    if (typeof sessionID !== "string" || !sessionID) return;

    // Navigate to the new session. Positional args: name +
    // params. (Object-literal form hit a TextNodeRenderable crash
    // last iteration — opencode's navigate signature is `(name,
    // params)`, NOT `({type, sessionID})`.)
    if (api.route?.navigate) {
      api.route.navigate("session", { sessionID });
    }

    // Drop the configured banner into the chat as the first
    // message — non-LLM. `session.prompt` accepts `noReply: true`
    // which stores the user message but does NOT trigger an AI
    // reply. So the LCARS welcome banner appears in chat history
    // (preserves org-llm identity), no system-prompt processing,
    // no model load, no token generation. Sub-millisecond cost.
    if (cfg.auto_session_prompt) {
      try {
        await api.client?.session?.prompt?.({
          sessionID,
          noReply: true,
          parts: [{ type: "text", text: cfg.auto_session_prompt }],
        });
      } catch {
        // Banner is optional. The session + navigation already
        // succeeded; missing banner just leaves an empty chat.
      }
    }

    // Phase 17.1l: pending-prompt restore. The slow-LLM watcher
    // stashes the user's stuck prompt to disk before applying the
    // cloud-routing fix; cli.py reads it on the next launch and
    // surfaces it via cfg.auto_session_pending_prompt. Resubmit
    // here WITHOUT noReply so the LLM actually responds — by this
    // point we're routing through cloud (fast), so the user gets
    // their answer without retyping.
    if (cfg.auto_session_pending_prompt) {
      try {
        await api.client?.session?.prompt?.({
          sessionID,
          parts: [{
            type: "text",
            text: cfg.auto_session_pending_prompt,
          }],
        });
      } catch {
        // If resubmit fails, the user can manually retype — they
        // can also see the original prompt in the welcome banner
        // OR we can preserve it by injecting as a separate
        // noReply message. For now, swallow.
      }
    }
  } catch {
    // Session creation or navigation failed — user stays on home
    // view. The visibility flag is already true so the manual
    // prompt is restored and they can type normally.
  }
}
