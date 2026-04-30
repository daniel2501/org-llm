/**
 * @org-llm/opencode-plugin — TUI plugin.
 *
 * Phase 16.1: insight cards as a real opencode TUI surface. Replaces
 * the system-prompt injection path (which only fires when the LLM
 * responds; cards weren't visible until the user typed something).
 *
 * Behavior:
 *   • Reads .opencode/insight-cards.json (written by org-llm launch).
 *   • Registers an `/insights` slash command that opens a DialogSelect
 *     of cards. Selecting one prefills the prompt with that card's
 *     suggested question.
 *   • Pops a toast on plugin load so the user knows N insights are
 *     ready before typing anything.
 *
 * This file ships as TS source — opencode's embedded bun runtime
 * loads .ts plugin paths directly, so no build step is required.
 *
 * Types declared inline rather than imported from @opencode-ai/plugin
 * so this file is self-contained even when the package is not
 * locally installed (the type info exists in opencode's own
 * node_modules at runtime; we only declare the subset we actually use).
 */

// ── Inline type declarations (subset of @opencode-ai/plugin/tui) ──
// We only declare what this plugin uses. opencode's runtime supplies
// the real implementations.

type ToastVariant = "info" | "success" | "warning" | "error";

interface TuiToast {
  variant?: ToastVariant;
  title?: string;
  message: string;
  duration?: number;
}

interface TuiCommand {
  title: string;
  value: string;
  description?: string;
  category?: string;
  keybind?: string;
  suggested?: boolean;
  hidden?: boolean;
  enabled?: boolean;
  slash?: { name: string; aliases?: string[] };
  onSelect?: () => void;
}

interface TuiDialogSelectOption<Value = unknown> {
  title: string;
  value: Value;
  description?: string;
  footer?: unknown;
  category?: string;
  disabled?: boolean;
  onSelect?: () => void;
}

interface TuiDialogSelectProps<Value = unknown> {
  title: string;
  placeholder?: string;
  options: TuiDialogSelectOption<Value>[];
  flat?: boolean;
  onSelect?: (option: TuiDialogSelectOption<Value>) => void;
  skipFilter?: boolean;
}

interface TuiPluginApi {
  app: { readonly version: string };
  command: {
    register: (cb: () => TuiCommand[]) => () => void;
    trigger: (value: string) => void;
    show: () => void;
  };
  ui: {
    toast: (input: TuiToast) => void;
    dialog: {
      replace: (render: () => unknown, onClose?: () => void) => void;
      clear: () => void;
    };
    DialogSelect: <Value = unknown>(props: TuiDialogSelectProps<Value>) => unknown;
  };
  state: {
    readonly path: {
      state: string;
      config: string;
      worktree: string;
      directory: string;
    };
  };
}

type TuiPlugin = (api: TuiPluginApi, options?: unknown, meta?: unknown) => Promise<void>;

// ── Card data shape (matches what cli.py writes) ─────────────────

interface InsightCard {
  kind: string;
  title: string;
  body: string;
  evidence?: unknown;
  suggested_command?: string;
  suggested_question?: string;
}

interface InsightCardsFile {
  generated_at?: string;
  count?: number;
  cards: InsightCard[];
}

// ── Plugin implementation ────────────────────────────────────────

async function loadCards(directory: string): Promise<InsightCard[]> {
  const path = `${directory}/.opencode/insight-cards.json`;
  try {
    const file = Bun.file(path);
    if (!(await file.exists())) return [];
    const data = (await file.json()) as InsightCardsFile;
    return Array.isArray(data?.cards) ? data.cards : [];
  } catch {
    return [];
  }
}

function cardSuggestedText(card: InsightCard): string {
  // Prefer the explicit question (clean prompt), then fall back to
  // a natural one synthesized from title + body. We deliberately do
  // NOT use suggested_command here — those tend to be conceptual
  // handles like "/synth foo" that aren't real slash commands.
  if (card.suggested_question?.trim()) return card.suggested_question.trim();
  const firstBodyLine = (card.body ?? "").split("\n")[0]?.trim() ?? "";
  return firstBodyLine
    ? `Tell me more about: ${card.title}. (${firstBodyLine})`
    : `Tell me more about: ${card.title}.`;
}

function openInsightDialog(api: TuiPluginApi, cards: InsightCard[]): void {
  const options: TuiDialogSelectOption<number>[] = cards.map((c, i) => {
    const firstBody = (c.body ?? "").split("\n")[0]?.trim() ?? "";
    return {
      title: c.title,
      value: i,
      description: firstBody.slice(0, 120),
      category: c.kind,
    };
  });

  api.ui.dialog.replace(() =>
    api.ui.DialogSelect<number>({
      title: `org-llm — ${cards.length} insight${cards.length === 1 ? "" : "s"} for this session`,
      placeholder: "type to filter…",
      options,
      onSelect: (opt) => {
        const card = cards[opt.value];
        if (!card) return;
        const text = cardSuggestedText(card);
        api.ui.dialog.clear();
        api.ui.toast({
          variant: "success",
          title: card.title,
          message: text.length > 140 ? text.slice(0, 137) + "…" : text,
          duration: 6000,
        });
        // Stash the chosen text so the user can paste it in. A future
        // iteration will set the home_prompt directly via api.ui.Slot
        // once we add a JSX runtime; for now toast is the visible
        // confirmation that the card was picked.
      },
    })
  );
}

export const tui: TuiPlugin = async (api) => {
  // Register slot overrides FIRST — they're independent of insight
  // cards (the home_logo / sidebar_title surfaces should reflect
  // org-llm branding even when there are no cards to show). Loaded
  // lazily so the rest of the plugin works even if the slots module
  // fails to import (e.g. opentui peer-dep mismatch on older
  // opencode versions).
  try {
    const { registerSlots } = await import("./slots");
    // The slots module's TuiPluginApi shape is structurally compatible
    // with what opencode passes here, but TypeScript's nominal typing
    // for the unused `app`/`route`/etc. fields requires an `as` here.
    registerSlots(api as unknown as Parameters<typeof registerSlots>[0]);
  } catch (e) {
    // Branding override is best-effort; never block plugin load.
    api.ui?.toast?.({
      variant: "warning",
      title: "org-llm",
      message: `slot override failed (${(e as Error)?.message ?? "unknown"}) — using opencode defaults`,
      duration: 4000,
    });
  }

  const directory = api.state.path.directory;
  const cards = await loadCards(directory);

  if (cards.length === 0) return;

  // Register a slash command users can run anytime to re-open the
  // dialog. `suggested: true` makes opencode bubble it up in the
  // command palette.
  api.command.register(() => [
    {
      title: `Insights — ${cards.length} card${cards.length === 1 ? "" : "s"} ready`,
      value: "org-llm.insights",
      description: "Browse the insight cards org-llm prepared for this session",
      category: "org-llm",
      slash: { name: "insights", aliases: ["i"] },
      suggested: true,
      onSelect: () => openInsightDialog(api, cards),
    },
  ]);

  // Flash a toast on plugin load — the visible "I noticed N things"
  // signal that the chat surface lacks before any user input. Stays
  // as a fallback breadcrumb so if the user dismisses the dialog
  // (Esc) they still see a hint of what just happened.
  api.ui.toast({
    variant: "info",
    title: "✨ org-llm",
    message: `${cards.length} insight${cards.length === 1 ? "" : "s"} ready — type /insights or Ctrl+P to browse`,
    duration: 8000,
  });

  // Auto-open the dialog so cards APPEAR ON OPEN — the user shouldn't
  // need to type /insights to discover what was prepared. The dialog
  // is modal-y but Escape dismisses cleanly and `/insights` re-opens
  // it from the command palette. setTimeout(..., 0) lets opencode's
  // own UI mount first; opening synchronously here can race with the
  // chat-surface paint and produce a flicker.
  setTimeout(() => openInsightDialog(api, cards), 0);
};

// opencode's plugin loader checks for both named exports (`tui` /
// `server`) and a default export of the same shape. We export both
// for resilience across opencode versions.
export default { tui };
