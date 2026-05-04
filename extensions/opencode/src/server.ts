/**
 * @org-llm/opencode-plugin · server-side hook
 *
 * Re-injects the @<agent> literal that opencode's TUI parses out
 * of user input. opencode 1.14.33 splits a turn like
 *
 *     @data Idea: cold-start latency …
 *
 * into separate parts: an `AgentPart` (with name="data" and
 * source.value="@data") plus a `TextPart` whose text is the body
 * with the prefix already stripped. The TextPart is what gets
 * serialised into messages[].content for the chat-completions
 * request that hits org-llm's proxy at port 43283. The proxy's
 * `intercept_agent_prefix` regex requires the body to start with
 * `@<name> ` — without the literal, no swap fires, and the
 * primary "Org-Llm" persona answers instead of the requested
 * specialist.
 *
 * Discovered during the W2 walkthrough on 2026-05-04. Sub-agent
 * report at the time documented this as the cleanest fix path
 * (~15 LoC, single hook, lowest blast radius). See also
 * docs/wiki/dev-tracker.org → friction log entry "@<agent>
 * per-turn routing dies inside opencode TUI" for context.
 *
 * The `chat.message` hook is mutable and fires before the LLM
 * call (verified via the {message, parts} output shape — only
 * makes sense pre-send; post-receive would be assistant-shaped).
 * We find the AgentPart, lift its source literal, and prepend it
 * to the first text part. opencode's autocomplete and visual
 * pill-rendering for @<name> stay intact; only the on-the-wire
 * body changes.
 *
 * NOTE: server plugins must NOT also export `tui` (PluginModule
 * shape forbids it — `tui?: never`). The TUI plugin lives in
 * src/index.ts and ships separately via tui.json's `plugin[]`;
 * this file ships via opencode.json's `plugin[]`.
 */

// Inline types — same defensive approach as src/index.ts: don't
// import from @opencode-ai/plugin so the file is self-contained.
// opencode's runtime supplies the real types when loading.

interface UserMessageLike {
  id: string;
  sessionID: string;
  role: "user";
}

interface TextPart {
  type: "text";
  text: string;
  // …other fields ignored
}

interface AgentPart {
  type: "agent";
  name: string;
  source?: { value: string; start: number; end: number };
}

interface ChatMessageInput {
  sessionID: string;
  agent?: string;
  model?: { providerID: string; modelID: string };
  messageID?: string;
  variant?: string;
}

interface ChatMessageOutput {
  message: UserMessageLike;
  parts: Array<TextPart | AgentPart | { type: string; [k: string]: unknown }>;
}

type Hooks = {
  "chat.message"?: (
    input: ChatMessageInput,
    output: ChatMessageOutput,
  ) => Promise<void>;
};

type ServerPlugin = (input: unknown, options?: unknown) => Promise<Hooks>;


export const server: ServerPlugin = async () => ({
  "chat.message": async (_input, output) => {
    const parts = output?.parts ?? [];
    if (!parts.length) return;

    // Find the AgentPart the TUI parsed from `@<name>`. There
    // should be at most one per turn (opencode strips the rest
    // as duplicates), but tolerate >1 by taking the first.
    const agentPart = parts.find(
      (p): p is AgentPart => (p as { type?: string })?.type === "agent",
    );
    if (!agentPart) return;

    const literal = agentPart.source?.value
      ?? `@${agentPart.name}`;

    // Find the first text part to re-prepend the literal.
    const firstText = parts.find(
      (p): p is TextPart => (p as { type?: string })?.type === "text",
    );
    if (!firstText || typeof firstText.text !== "string") return;

    const trimmed = firstText.text.replace(/^\s+/, "");
    // Idempotency: if the literal is already at the front (e.g.
    // hook re-runs, or some other transform already prepended),
    // don't double-add.
    if (
      trimmed.startsWith(literal + " ") ||
      trimmed.startsWith(literal + "\n") ||
      trimmed === literal
    ) {
      return;
    }

    firstText.text = literal + " " + trimmed;
  },
});

export const id = "@org-llm/opencode-plugin-server";

export default { id, server };
